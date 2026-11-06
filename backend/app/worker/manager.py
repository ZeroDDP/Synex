"""Запуск synex_worker и присмотр за ним.

* Бинарник запускается с теми же RUST_IPC_* значениями, что видит Python, поэтому
  обе стороны всегда сходятся на одном имени pipe/сокета и порту.
* stdout/stderr воркера читаются в отдельных потоках. Если их не читать, буфер пайпа
  (~4 КБ на Windows) переполняется и воркер виснет на очередном println!.
* Менеджер ждёт строку "Listening on pipe|tcp ..." и подключается именно к тому
  транспорту, который выбрал воркер. Воркер печатает "Listening on pipe" до того, как
  создаёт pipe, и при неудаче уходит на TCP следующей строкой, поэтому клиент
  перечитывает выбранный транспорт перед каждой попыткой.
* Вывод воркера (логи tracing) очищается от ANSI-цветов, уровень берётся из строки.
  Пароль и парольная фраза, которые воркер печатает при ssh_connect, затираются до
  записи в лог.
* Супервизор: если процесс умер, публикуется событие worker.status/worker_exited и
  воркер перезапускается с экспоненциальной задержкой. Если оборвалось только
  соединение, менеджер переподключается.
* На Windows воркер помещается в Job Object с KILL_ON_JOB_CLOSE: если Python упадёт
  или его убьют из IDE, воркер умрёт вместе с ним и не будет держать имя pipe. На Linux
  и macOS то же самое делает process.py (своя сессия, PDEATHSIG, PID-файл), а сокет
  готовит unix_socket.py.
* Кроме основного подключения (client) менеджер открывает дополнительные
  (open_channel): воркер выполняет запросы одного подключения по очереди, а разных
  параллельно, поэтому у каждой SSH-сессии своё подключение. При остановке менеджер
  закрывает их все.
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
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

import structlog

from app.core.config import default_socket_path, settings
from app.core.errors import WorkerConnectionError
from app.core.event_bus import event_bus
from app.worker.client import WorkerClient
from app.worker.executable import (
    candidates,
    ensure_executable,
    find_executable,
    host_arch,
    host_os,
    startup_failure_hint,
)
from app.worker.process import WorkerPidFile, popen_options
from app.worker.unix_socket import SocketLocation

logger = structlog.get_logger()

WORKER_STATUS_EVENT = "worker.status"
# Значения payload["status"] в событии worker.status
STATUS_CONNECTED = "connected"
STATUS_CONNECTION_LOST = "connection_lost"
STATUS_WORKER_STARTED = "worker_started"
STATUS_WORKER_EXITED = "worker_exited"
# При connection_lost и worker_exited SSH-сессии в воркере потеряны: воркер закрывает
# сессии подключения, которое оборвалось.

SUPERVISE_INTERVAL = 2.0
READY_WAIT = 3.0  # сколько ждать строку "Listening on ..." перед попыткой подключения
RECONNECT_TIMEOUT = 2.0
CHANNEL_CONNECT_TIMEOUT = 3.0  # сколько ждать дополнительное подключение (open_channel)
# Процесс жив, но IPC недоступен столько проверок подряд (~20 с): считаем воркер
# зависшим и перезапускаем.
MAX_FAILED_RECONNECTS = 5
MAX_RESTART_DELAY = 30.0
STABLE_UPTIME = 60.0  # после стольких секунд без падений задержка рестарта сбрасывается

_LISTEN_RE = re.compile(r"listening on (pipe|tcp|unix|socket)", re.IGNORECASE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# Строка tracing: "2026-11-03T11:18:26.796342Z ERROR synex_worker: текст"
_TRACING_RE = re.compile(r"^\S+\s+(TRACE|DEBUG|INFO|WARN|ERROR)\s+\S+?:\s?(.*)$")
# Всё после "password:" / "passphrase:" до конца строки: значение может содержать
# запятые и скобки, поэтому безопаснее затереть хвост целиком.
_SECRET_RE = re.compile(r"\b(passphrase|password)\b\s*[:=].*$", re.IGNORECASE)
_PANIC_RE = re.compile(r"\b(panicked|fatal)\b", re.IGNORECASE)
_WARN_RE = re.compile(r"^pipe failed|falling back|❌|⚠", re.IGNORECASE)


def sanitize_worker_line(raw: str) -> str:
    """Убирает ANSI-цвета и секреты из строки вывода воркера."""
    line = _ANSI_RE.sub("", raw).rstrip()
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}: <hidden>", line)


def worker_line_level(line: str) -> str:
    """Уровень, с которым строка вывода воркера попадёт в лог backend."""
    if _LISTEN_RE.search(line):
        return "info"
    match = _TRACING_RE.match(line)
    if match:
        # ERROR у воркера это обычно ошибка запроса ("Session not found", неверный
        # пароль): она и так вернётся в API, поэтому в логе backend это warning.
        return "warning" if match.group(1) in ("ERROR", "WARN") else "debug"
    if _PANIC_RE.search(line):
        return "error"
    if _WARN_RE.search(line):
        return "warning"
    # Остальное, в том числе продолжение многострочных команд, только в DEBUG
    return "debug"


class WorkerManager:
    """Владеет процессом synex_worker, основным WorkerClient и дополнительными
    подключениями к нему (open_channel)."""

    def __init__(self) -> None:
        self.client: WorkerClient | None = None
        self._channels: weakref.WeakSet[WorkerClient] = weakref.WeakSet()
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
        self._socket: SocketLocation | None = None  # Linux/macOS: путь и папка сокета
        self._pid_file: WorkerPidFile | None = None  # Linux/macOS: воркер прошлого запуска

    # ------------------------------------------------------------- public API

    async def start(self) -> None:
        self._stopping = False
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        if sys.platform != "win32":
            self._socket = SocketLocation(settings.RUST_IPC_SOCKET_PATH, default_socket_path())
            socket_path = self._socket.choose()
            self._pid_file = WorkerPidFile(socket_path.with_name(socket_path.name + ".pid"))
            # Воркер, оставшийся от прошлого запуска (backend убили), держит сокет и порт
            await asyncio.to_thread(self._pid_file.stop_orphan)

        client = WorkerClient(socket_path=self.socket_path)
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

        for channel in list(self._channels):
            with contextlib.suppress(Exception):
                await channel.disconnect()
        if self.client is not None:
            with contextlib.suppress(Exception):
                await self.client.disconnect()

        await self._terminate_worker()
        if self._socket is not None:
            self._socket.cleanup()
        self._close_job()
        self.client = None
        logger.info("Rust worker manager stopped")

    def status(self) -> dict[str, Any]:
        client = self.client.status() if self.client is not None else {"connected": False}
        proc = self._process
        alive = proc is not None and proc.poll() is None
        return {
            **client,
            "channels": sum(channel.connected for channel in list(self._channels)),
            "worker_pid": proc.pid if alive and proc is not None else None,
            "worker_restarts": self._restarts,
        }

    async def open_channel(self, *, timeout: float = CHANNEL_CONNECT_TIMEOUT) -> WorkerClient:
        """Ещё одно IPC-подключение к тому же воркеру, тем же транспортом.

        Его запросы выполняются параллельно с запросами других подключений, а SSH-сессии,
        открытые через него, принадлежат только ему: воркер закроет их, когда подключение
        закроется. Закрывает подключение тот, кто его открыл (disconnect()).
        """
        main = self.client
        if main is None or not main.connected:
            raise WorkerConnectionError("Rust worker is not connected")
        channel = WorkerClient(socket_path=self.socket_path)
        await channel.connect(timeout=timeout, prefer=main.transport)
        self._channels.add(channel)
        return channel

    @property
    def socket_path(self) -> str:
        """Unix socket воркера (Linux/macOS), тот же, что передан ему в окружении."""
        if self._socket is not None:
            return str(self._socket.path)
        return settings.ipc_socket_path

    @property
    def worker_output_tail(self) -> list[str]:
        return list(self._output_tail)

    @staticmethod
    def executable_candidates() -> list[Path]:
        """Где ищется бинарник (см. executable.py): сначала относительно backend/."""
        return candidates(
            settings.rust_executable_path,
            settings.BASE_DIR,
            include_core=settings.rust_executable_path_is_default,
        )

    # ----------------------------------------------------------- spawn / stop

    def _worker_command(self) -> list[str] | None:
        found = find_executable(
            settings.rust_executable_path,
            settings.BASE_DIR,
            include_core=settings.rust_executable_path_is_default,
        )
        if found.path is not None:
            return [str(found.path)] if ensure_executable(found.path) else None
        if found.skipped:
            logger.error(
                "Rust worker executable is not built for this platform. Build it with "
                "`cargo build --release` or set RUST_EXECUTABLE_PATH_UNIX / _WIN32",
                platform=f"{host_os()}-{host_arch()}",
                skipped=found.skipped,
            )
        logger.warning(
            "Rust worker executable not found. Will try to connect to an already running worker.",
            tried=found.tried,
        )
        return None

    def _worker_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "RUST_BRIDGE_MODE": "ipc",
                "RUST_IPC_PIPE_NAME": settings.RUST_IPC_PIPE_NAME,
                "RUST_IPC_SOCKET_PATH": self.socket_path,
                "RUST_IPC_TCP_FALLBACK_PORT": str(settings.RUST_IPC_TCP_FALLBACK_PORT),
            }
        )
        return env

    def _spawn_worker(self) -> bool:
        command = self._worker_command()
        if command is None:
            return False
        if self._socket is not None:
            self._socket.prepare()  # до запуска: папка 0700, мёртвый сокет удалён

        self._listening = asyncio.Event()
        self._listening_transport = None
        self._output_tail.clear()
        try:
            proc = subprocess.Popen(
                command,
                env=self._worker_env(),
                # stdin держим открытым: ОС закроет его, когда backend завершится любым
                # способом, и воркер увидит EOF (process.py)
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **popen_options(),
            )
        except OSError as e:
            logger.error("Failed to start Rust worker", error=repr(e), command=command)
            self._process = None
            return False

        self._process = proc
        self._started_at = time.monotonic()
        self._attach_to_job(proc)
        if self._pid_file is not None:
            self._pid_file.write(proc.pid, command[0])
        self._start_pumps(proc)
        logger.info("Rust worker started", pid=proc.pid, path=command[0])
        return True

    async def _terminate_worker(self) -> None:
        proc, self._process = self._process, None
        if proc is None:
            return

        self._close_stdin(proc)
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
        if self._pid_file is not None:
            self._pid_file.clear(proc.pid)
        logger.info("Rust worker stopped", pid=proc.pid, returncode=proc.returncode)

    @staticmethod
    def _close_stdin(proc: subprocess.Popen[bytes]) -> None:
        if proc.stdin is not None:
            with contextlib.suppress(OSError):
                proc.stdin.close()

    # ------------------------------------------------------------ connection

    async def _connect(self, *, initial: bool = False) -> bool:
        client = self.client
        if client is None or client.connected:
            return True

        prefer: Callable[[], str | None] | None = None
        proc = self._process
        if proc is not None:
            if not self._listening.is_set():
                await self._wait_listening(proc, READY_WAIT)
            prefer = self._current_transport
            if proc.poll() is not None:
                if initial:
                    hint = startup_failure_hint(proc.returncode, self.worker_output_tail)
                    logger.error(
                        "Rust worker exited during startup",
                        returncode=proc.returncode,
                        last_output=self.worker_output_tail[-15:],
                        **({"hint": hint} if hint else {}),
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

    def _current_transport(self) -> str | None:
        return self._listening_transport

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
        self._close_stdin(proc)
        if self._pid_file is not None:
            self._pid_file.clear(proc.pid)
        await asyncio.to_thread(self._join_pumps)
        hint = startup_failure_hint(returncode, self.worker_output_tail)
        logger.error(
            "Rust worker exited",
            pid=proc.pid,
            returncode=returncode,
            last_output=self.worker_output_tail[-15:],
            **({"hint": hint} if hint else {}),
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
                line = sanitize_worker_line(raw.decode("utf-8", errors="replace"))
                if not line:
                    continue
                self._output_tail.append(f"[{name}] {line}")

                match = _LISTEN_RE.search(line)
                if match:
                    transport = match.group(1).lower()
                    self._notify_listening("unix" if transport == "socket" else transport)
                try:
                    self._log_output(name, pid, line)
                except Exception:
                    # Например, UnicodeEncodeError: эмодзи воркера в stdout с кодировкой cp1251
                    pass
        except OSError:
            pass  # пайп закрыт: процесс завершился
        finally:
            with contextlib.suppress(Exception):
                stream.close()

    @staticmethod
    def _log_output(name: str, pid: int, line: str) -> None:
        # Служебный вывод на каждый запрос (в т.ч. текст команд) только в DEBUG
        log = getattr(logger, worker_line_level(line))
        log("Rust worker output", stream=name, pid=pid, line=line)

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
