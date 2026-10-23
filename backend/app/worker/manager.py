"""Запуск synex_worker и присмотр за ним.

* Бинарник запускается с теми же RUST_IPC_* значениями, что видит Python, поэтому
  обе стороны всегда сходятся на одном имени pipe/сокета и порту.
* stdout/stderr воркера читаются в отдельных потоках. Если их не читать, буфер пайпа
  (~4 КБ на Windows) переполняется и воркер виснет на очередном println!.
* Менеджер ждёт строку "Listening on pipe|tcp ..." и подключается именно к тому
  транспорту, который выбрал воркер.
* Супервизор: если процесс умер, публикуется событие worker.status/worker_exited и
  воркер перезапускается с экспоненциальной задержкой. Если оборвалось только
  соединение, менеджер переподключается.
* На Windows воркер помещается в Job Object с KILL_ON_JOB_CLOSE: если Python упадёт
  или его убьют из IDE, воркер умрёт вместе с ним и не будет держать имя pipe.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, Any

import structlog

from app.core.config import settings
from app.core.errors import WorkerConnectionError
from app.core.event_bus import event_bus
from app.worker.client import WorkerClient

logger = structlog.get_logger()

WORKER_STATUS_EVENT = "worker.status"
# Значения payload["status"] в событии worker.status
STATUS_CONNECTED = "connected"
STATUS_CONNECTION_LOST = "connection_lost"
STATUS_WORKER_STARTED = "worker_started"
STATUS_WORKER_EXITED = "worker_exited"  # SSH-сессия внутри воркера потеряна

SUPERVISE_INTERVAL = 2.0
READY_WAIT = 3.0  # сколько ждать строку "Listening on ..." перед попыткой подключения
RECONNECT_TIMEOUT = 2.0
# Процесс жив, но IPC недоступен столько проверок подряд (~20 с): считаем воркер
# зависшим и перезапускаем.
MAX_FAILED_RECONNECTS = 5
MAX_RESTART_DELAY = 30.0
STABLE_UPTIME = 60.0  # после стольких секунд без падений задержка рестарта сбрасывается

_LISTEN_RE = re.compile(r"listening on (pipe|tcp|unix|socket)", re.IGNORECASE)
_ERROR_RE = re.compile(r"\b(error|panicked|fatal)\b", re.IGNORECASE)
_WARN_RE = re.compile(r"\b(warn|warning|failed)\b|❌|⚠", re.IGNORECASE)


class WorkerManager:
    """Владеет процессом synex_worker и единственным WorkerClient к нему."""

    def __init__(self) -> None:
        self.client: WorkerClient | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._pump_threads: list[threading.Thread] = []
        self._output_tail: collections.deque[str] = collections.deque(maxlen=50)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._listening = asyncio.Event()
        self._listening_transport: str | None = None
        self._wake = asyncio.Event()
        self._supervisor: asyncio.Task | None = None
        self._background: set[asyncio.Task] = set()
        self._stopping = False
        self._started_at = 0.0
        self._restart_delay = 1.0
        self._restarts = 0
        self._connect_failures = 0
        self._job: Any = None

    # ------------------------------------------------------------- public API

    async def start(self) -> None:
        self._stopping = False
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()

        client = WorkerClient()
        client.add_disconnect_listener(self._on_connection_lost)
        self.client = client

        self._spawn_worker()
        await self._connect(initial=True)
        self._supervisor = asyncio.create_task(self._supervise(), name="rust-worker-supervisor")

    async def stop(self) -> None:
        logger.info("Stopping Rust worker...")
        self._stopping = True
        self._wake.set()

        task, self._supervisor = self._supervisor, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        if self.client is not None:
            with contextlib.suppress(Exception):
                await self.client.disconnect()

        await self._terminate_worker()
        self._close_job()
        self.client = None
        logger.info("Rust worker manager stopped")

    def status(self) -> dict[str, Any]:
        client = self.client.status() if self.client is not None else {"connected": False}
        proc = self._process
        alive = proc is not None and proc.poll() is None
        return {
            **client,
            "worker_pid": proc.pid if alive and proc is not None else None,
            "worker_restarts": self._restarts,
        }

    @property
    def worker_output_tail(self) -> list[str]:
        return list(self._output_tail)

    @staticmethod
    def executable_candidates() -> list[Path]:
        raw = Path(settings.rust_executable_path)
        if raw.is_absolute():
            return [raw]
        # Сначала относительно backend/ (не зависит от того, откуда запущен Python),
        # затем относительно cwd, как было раньше.
        return [settings.BASE_DIR / raw, Path.cwd() / raw]

    # ----------------------------------------------------------- spawn / stop

    def _worker_command(self) -> list[str] | None:
        candidates = self.executable_candidates()
        for path in candidates:
            if path.is_file():
                return [str(path.resolve())]
        logger.warning(
            "Rust worker executable not found. Will try to connect to an already running worker.",
            tried=[str(p) for p in candidates],
        )
        return None

    def _worker_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "RUST_BRIDGE_MODE": "ipc",
                "RUST_IPC_PIPE_NAME": settings.RUST_IPC_PIPE_NAME,
                "RUST_IPC_SOCKET_PATH": settings.RUST_IPC_SOCKET_PATH,
                "RUST_IPC_TCP_FALLBACK_PORT": str(settings.RUST_IPC_TCP_FALLBACK_PORT),
            }
        )
        return env

    def _spawn_worker(self) -> bool:
        command = self._worker_command()
        if command is None:
            return False

        self._listening = asyncio.Event()
        self._listening_transport = None
        self._output_tail.clear()
        try:
            proc = subprocess.Popen(
                command,
                env=self._worker_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except OSError as e:
            logger.error("Failed to start Rust worker", error=repr(e), command=command)
            self._process = None
            return False

        self._process = proc
        self._started_at = time.monotonic()
        self._attach_to_job(proc)
        self._start_pumps(proc)
        logger.info("Rust worker started", pid=proc.pid, path=command[0])
        return True

    async def _terminate_worker(self) -> None:
        proc, self._process = self._process, None
        if proc is None:
            return

        if proc.poll() is None:
            logger.info("Terminating Rust worker", pid=proc.pid)
            proc.terminate()
            try:
                await asyncio.to_thread(proc.wait, 3.0)
            except subprocess.TimeoutExpired:
                logger.warning("Rust worker did not stop in time, killing it", pid=proc.pid)
                proc.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    await asyncio.to_thread(proc.wait, 2.0)

        await asyncio.to_thread(self._join_pumps)
        logger.info("Rust worker stopped", pid=proc.pid, returncode=proc.returncode)

    # ------------------------------------------------------------ connection

    async def _connect(self, *, initial: bool = False) -> bool:
        client = self.client
        if client is None or client.connected:
            return True

        prefer: str | None = None
        proc = self._process
        if proc is not None:
            if not self._listening.is_set():
                await self._wait_listening(proc, READY_WAIT)
            prefer = self._listening_transport
            if proc.poll() is not None:
                if initial:
                    logger.error(
                        "Rust worker exited during startup",
                        returncode=proc.returncode,
                        last_output=self.worker_output_tail[-15:],
                    )
                self._wake.set()  # супервизор обработает выход и перезапустит
                return False

        timeout = settings.RUST_IPC_CONNECT_TIMEOUT if initial else RECONNECT_TIMEOUT
        try:
            await client.connect(timeout=timeout, prefer=prefer)
        except WorkerConnectionError as e:
            self._connect_failures += 1
            if initial or self._connect_failures % 30 == 1:
                logger.warning(
                    "Rust worker IPC is not reachable yet, will keep retrying",
                    error=e.detail,
                    last_output=self.worker_output_tail[-5:],
                )
            return False

        self._connect_failures = 0
        await self._publish(STATUS_CONNECTED, transport=client.transport)
        return True

    async def _wait_listening(self, proc: subprocess.Popen[bytes], timeout: float) -> None:
        """Ждёт строку "Listening on ..." от воркера, его падение или таймаут."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not self._listening.is_set() and proc.poll() is None and loop.time() < deadline:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._listening.wait(), timeout=0.1)

    async def _on_connection_lost(self, reason: str) -> None:
        self._wake.set()
        await self._publish(STATUS_CONNECTION_LOST, reason=reason)

    # ------------------------------------------------------------- supervisor

    async def _supervise(self) -> None:
        while not self._stopping:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=SUPERVISE_INTERVAL)
            self._wake.clear()
            if self._stopping:
                break
            try:
                await self._check()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Rust worker supervisor error", error=repr(e))

    async def _check(self) -> None:
        proc = self._process
        if proc is not None and proc.poll() is not None:
            await self._handle_worker_exit(proc)
            return

        if proc is not None and time.monotonic() - self._started_at > STABLE_UPTIME:
            self._restart_delay = 1.0

        if self.client is None or self.client.connected:
            return

        if proc is not None:
            # Соединение обычно рвётся чуть раньше, чем процесс помечается завершённым.
            with contextlib.suppress(subprocess.TimeoutExpired):
                await asyncio.to_thread(proc.wait, 0.5)
            if proc.poll() is not None:
                await self._handle_worker_exit(proc)
                return

        if await self._connect():
            return

        if (
            proc is not None
            and proc.poll() is None
            and settings.RUST_WORKER_AUTORESTART
            and self._connect_failures >= MAX_FAILED_RECONNECTS
        ):
            logger.error(
                "Rust worker is running but its IPC is unreachable, restarting it",
                pid=proc.pid,
                failed_attempts=self._connect_failures,
                last_output=self.worker_output_tail[-10:],
            )
            self._connect_failures = 0
            proc.kill()
            self._wake.set()  # следующая проверка увидит выход и перезапустит воркер

    async def _handle_worker_exit(self, proc: subprocess.Popen[bytes]) -> None:
        returncode = proc.returncode
        self._process = None
        await asyncio.to_thread(self._join_pumps)
        logger.error(
            "Rust worker exited",
            pid=proc.pid,
            returncode=returncode,
            last_output=self.worker_output_tail[-15:],
        )
        if self.client is not None:
            await self.client.disconnect()
        await self._publish(STATUS_WORKER_EXITED, returncode=returncode)

        if self._stopping or not settings.RUST_WORKER_AUTORESTART:
            return

        delay = self._restart_delay
        self._restart_delay = min(delay * 2, MAX_RESTART_DELAY)
        logger.info("Restarting Rust worker", delay_seconds=delay)
        await asyncio.sleep(delay)
        if self._stopping:
            return

        if self._spawn_worker():
            self._restarts += 1
            pid = self._process.pid if self._process else None
            await self._publish(STATUS_WORKER_STARTED, pid=pid, restarts=self._restarts)
            await self._connect(initial=True)

    async def _publish(self, status: str, **extra: Any) -> None:
        event = {
            "event_type": WORKER_STATUS_EVENT,
            "payload": {"status": status, **extra},
            "timestamp": time.time(),
        }
        try:
            await event_bus.publish(WORKER_STATUS_EVENT, event)
        except Exception as e:
            logger.error("Failed to publish worker status", error=repr(e))

    # ----------------------------------------------------------- output pumps

    def _start_pumps(self, proc: subprocess.Popen[bytes]) -> None:
        self._pump_threads = []
        for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            if stream is None:
                continue
            thread = threading.Thread(
                target=self._pump,
                args=(stream, name, proc.pid),
                name=f"rust-worker-{name}",
                daemon=True,
            )
            thread.start()
            self._pump_threads.append(thread)

    def _pump(self, stream: IO[bytes], name: str, pid: int) -> None:
        """Читает вывод воркера до EOF. Останавливаться раньше нельзя ни при какой ошибке:
        непрочитанный пайп переполнится, и воркер зависнет на очередном println!."""
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                self._output_tail.append(f"[{name}] {line}")

                match = _LISTEN_RE.search(line)
                if match:
                    transport = match.group(1).lower()
                    self._notify_listening("unix" if transport == "socket" else transport)
                try:
                    self._log_output(name, pid, line, listening=match is not None)
                except Exception:
                    # Например, UnicodeEncodeError: эмодзи воркера в stdout с кодировкой cp1251
                    pass
        except OSError:
            pass  # пайп закрыт: процесс завершился
        finally:
            with contextlib.suppress(Exception):
                stream.close()

    @staticmethod
    def _log_output(name: str, pid: int, line: str, *, listening: bool) -> None:
        if listening:
            logger.info("Rust worker output", stream=name, pid=pid, line=line)
        elif _ERROR_RE.search(line):
            logger.error("Rust worker output", stream=name, pid=pid, line=line)
        elif _WARN_RE.search(line):
            logger.warning("Rust worker output", stream=name, pid=pid, line=line)
        else:
            # Служебный вывод на каждый запрос (в т.ч. текст команд) только в DEBUG
            logger.debug("Rust worker output", stream=name, pid=pid, line=line)

    def _notify_listening(self, transport: str) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        listening = self._listening

        def mark() -> None:
            self._listening_transport = transport
            listening.set()

        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(mark)

    def _join_pumps(self) -> None:
        for thread in self._pump_threads:
            thread.join(timeout=1.0)
        self._pump_threads = []

    # -------------------------------------------------------- windows job obj

    def _attach_to_job(self, proc: subprocess.Popen[bytes]) -> None:
        if sys.platform != "win32":
            return
        try:
            import win32api  # type: ignore[import-not-found]
            import win32con  # type: ignore[import-not-found]
            import win32job  # type: ignore[import-not-found]

            if self._job is None:
                job = win32job.CreateJobObject(None, "")
                info = win32job.QueryInformationJobObject(
                    job, win32job.JobObjectExtendedLimitInformation
                )
                info["BasicLimitInformation"]["LimitFlags"] |= (
                    win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                )
                win32job.SetInformationJobObject(
                    job, win32job.JobObjectExtendedLimitInformation, info
                )
                self._job = job

            handle = win32api.OpenProcess(
                win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, proc.pid
            )
            try:
                win32job.AssignProcessToJobObject(self._job, handle)
            finally:
                handle.Close()
        except Exception as e:
            logger.info(
                "Could not bind Rust worker to a job object; "
                "it may outlive the backend if Python is killed",
                error=repr(e),
            )

    def _close_job(self) -> None:
        job, self._job = self._job, None
        if job is not None:
            with contextlib.suppress(Exception):
                job.Close()


worker_manager = WorkerManager()
