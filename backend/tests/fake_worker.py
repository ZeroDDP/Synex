"""Фейковый synex_worker для тестов. Повторяет поведение сборки от 2026-11-03.

Как настоящий воркер:
  * `id` запроса это ключ SSH-сессии: ssh_connect с id X создаёт сессию X, ssh_exec,
    monitor_get_values и ssh_disconnect с тем же id работают с ней;
  * сессии принадлежат IPC-подключению и закрываются вместе с ним;
  * запросы одного подключения выполняются по очереди (sequential=False отключает);
  * exit_code всегда 0 (lose_exit_status=False отдаёт настоящий код);
  * stdout отдаётся как есть, с переводом строки в конце;
  * при ssh_connect печатает в stdout строку лога с паролем и парольной фразой.

Используется двумя способами:
  * как asyncio.Protocol внутри теста (TCP, Unix socket или Named Pipe);
  * отдельным процессом: `python tests/fake_worker.py`. Слушает TCP 127.0.0.1 на
    RUST_IPC_TCP_FALLBACK_PORT и печатает те же строки, что настоящий воркер после
    перехода с pipe на TCP. С FAKE_WORKER_TRANSPORT=unix (Linux/macOS) слушает
    RUST_IPC_SOCKET_PATH; если файл по этому пути уже есть, уходит на TCP.

SSH-сервер воображаемый:
  * host "unreachable": "Connection failed: Connection refused";
  * password: подходит только PASSWORD;
  * key_path: файл должен существовать; если в нём есть "ENCRYPTED", нужна парольная
    фраза PASSPHRASE.

monitor_get_values, как сборка с исправленным CPU: первый вызов в сессии отдаёт CPU «от
загрузки сервера» (BOOT_CPU), следующие 10, 11, 12 ... Хост "no-proc" имитирует сервер,
где команда метрик не выполнилась; FakeWorkerState.monitor_error делает ok=false.

SFTP (sftp_*) как сборка от 2026-11-12: тексты ошибок, "success" у sftp_download_file,
move без текста ошибки, copy папки без -r «успешно» ничего не копирует. Файловая
система сервера: с эмулятором shell это его виртуальная ФС (FakeShell.root, домашняя
папка /home/synex), иначе настоящая ФС этой машины (домашняя папка home). Пути, которые
не начинаются с "/", считаются от домашней папки, "~" воркер не раскрывает.
FakeWorkerState.sftp_delay: пауза на каждый мегабайт передачи (проверка прогресса).

Служебные action для тестов клиента:
  test_sleep {seconds, echo}    ответ с задержкой
  test_big {size}               большой stdout
  test_close                    воркер закрывает соединение без ответа
  test_not_a_map                фрейм-список вместо словаря (как при to_vec)
  test_crash                    процесс завершается с кодом 3

FAKE_WORKER_ACCEPT_ONCE=1: после первого клиента перестаёт принимать соединения
(имитирует воркер, который не умеет переподключение).
"""

from __future__ import annotations

import asyncio
import json
import os
import posixpath
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import msgpack

if TYPE_CHECKING:
    from tests.fake_shell import FakeShell

PASSWORD = "right-password"
BOOT_CPU = 2.63  # CPU в первом замере сессии: среднее с загрузки сервера
MEM_TOTAL_KB = 2014404
PASSPHRASE = "right-phrase"
NO_SESSION = "Session dont founded by key. Create session first!"

# Пример строки лога tracing настоящего воркера (с ANSI-цветами)
LOG_LINE = (
    "\x1b[2m2026-11-03T11:18:26.840998Z\x1b[0m \x1b[32m INFO\x1b[0m "
    "\x1b[2msynex_worker\x1b[0m\x1b[2m:\x1b[0m {}"
)


def frame(message: Any) -> bytes:
    data = msgpack.packb(message, use_bin_type=True)
    return len(data).to_bytes(4, "big") + data


def reply(msg_id: str, status: str, stdout: str = "", stderr: str = "", exit_code: int = 0):
    return {
        "id": msg_id,
        "status": status,
        "payload": {"stdout": stdout, "stderr": stderr, "exit_code": exit_code},
    }


# Что выдал бы сервер на команду дисков и GPU мониторинга (df -kP; nvidia-smi)
HARDWARE_OUTPUT = """Filesystem     1024-blocks     Used Available Capacity Mounted on
/dev/sda1         41152736 20576368  18463232      53% /
tmpfs              1024000        0   1024000       0% /dev/shm
/dev/sdb1        102400000 51200000  51200000      50% /data
__SYNEX_GPU__
0, NVIDIA GeForce RTX 3080, 37, 2048, 10240, 61
"""


class FakeWorkerState:
    def __init__(
        self,
        *,
        run_shell: bool = False,
        home: str | None = None,
        emulate_shell: bool = False,
        lose_exit_status: bool = True,
        sequential: bool = True,
        log_to_stdout: bool = False,
        connect_delay: float = 0.0,
        monitor_error: str | None = None,
        hardware_output: str | None = None,
    ) -> None:
        self.run_shell = run_shell
        self.connect_delay = connect_delay
        self.monitor_error = monitor_error  # ok=false с этим текстом в monitor_get_values
        # Ответ на команду дисков и GPU мониторинга (без run_shell), см. HARDWARE_OUTPUT
        self.hardware_output = HARDWARE_OUTPUT if hardware_output is None else hardware_output
        self.sequential = sequential
        self.lose_exit_status = lose_exit_status
        self.home = home
        # Команды выполняет FakeShell (Windows, SYNEX_TEST_SHELL=emulated) или `sh -c`.
        # shell_home: домашняя папка так, как её видит shell (что печатает pwd).
        self.shell: FakeShell | None = None
        self.shell_home = str(Path(home).resolve()) if home else None
        if emulate_shell:
            if home is None:
                raise ValueError("emulate_shell needs home: the root of the virtual FS")
            from tests.fake_shell import FakeShell  # процессу-воркеру эмулятор не нужен

            self.shell = FakeShell(Path(home))
            self.shell_home = self.shell.home
        self.log_to_stdout = log_to_stdout
        # Содержимое файла ключа на момент ssh_connect (None, если файла нет)
        self.connect_key_files: list[bytes | None] = []
        self.requests: list[dict[str, Any]] = []
        self.protocols: list[FakeWorkerProtocol] = []
        self.port: int | None = None
        self.server: asyncio.Server | None = None
        self.accept_once = False
        self.sftp_delay = 0.0  # секунд на каждый мегабайт sftp_upload/download
        self.sftp_fail: dict[str, str] = {}  # action -> текст ошибки (сбой по заказу)
        # Сбой по условию: (action, payload) -> текст ошибки или None (без сбоя)
        self.sftp_fail_if: Callable[[str, dict[str, Any]], str | None] | None = None

    def remote(self, path: str) -> Path:
        """Файл сервера для пути SFTP."""
        if self.shell is not None:
            root, home = self.shell.root, self.shell.home
        else:
            root, home = Path(Path.cwd().anchor or "/"), self.shell_home or str(Path.home())
        absolute = path if path.startswith("/") else posixpath.join(home, path)
        parts = [p for p in posixpath.normpath(absolute).split("/") if p]
        if self.shell is None and sys.platform != "win32":
            return Path("/", *parts)
        return root.joinpath(*parts)

    @property
    def sessions(self) -> set[str]:
        """Все открытые сессии всех подключений."""
        return {sid for protocol in self.protocols for sid in protocol.sessions}

    def drop_sessions(self) -> None:
        """Сессии пропали внутри воркера (как после его перезапуска)."""
        for protocol in self.protocols:
            protocol.sessions.clear()

    def close_all(self) -> None:
        for protocol in self.protocols:
            if protocol.transport is not None:
                protocol.transport.close()


class FakeWorkerProtocol(asyncio.Protocol):
    def __init__(self, state: FakeWorkerState) -> None:
        self.state = state
        self.buffer = bytearray()
        self.transport: asyncio.BaseTransport | None = None
        self.tasks: set[asyncio.Task] = set()
        self.sftp_tasks: set[asyncio.Task] = set()
        self.queue_lock = asyncio.Lock()
        self.sessions: dict[str, dict[str, Any]] = {}
        state.protocols.append(self)

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport
        if self.state.accept_once and self.state.server is not None:
            self.state.server.close()  # новые соединения больше не принимаются

    def connection_lost(self, exc: Exception | None) -> None:
        self.sessions.clear()  # авто-закрытие сессий после отключения Python
        # Действия SFTP этого подключения прерываются и закрывают файлы. Настоящий
        # воркер (сборка 2026-11-12) пока докачивает файл в фоне до конца, но для backend
        # итог тот же: ответа не будет. А открытый файл на Windows не удалить
        for task in self.sftp_tasks:
            task.cancel()

    def data_received(self, data: bytes) -> None:
        self.buffer += data
        while len(self.buffer) >= 4:
            length = int.from_bytes(self.buffer[:4], "big")
            if len(self.buffer) < 4 + length:
                break
            body = bytes(self.buffer[4 : 4 + length])
            del self.buffer[: 4 + length]
            task = asyncio.ensure_future(self.handle(body))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    def send(self, message: Any) -> None:
        transport = self.transport
        if transport is not None and not transport.is_closing():
            transport.write(frame(message))  # type: ignore[attr-defined]

    async def handle(self, body: bytes) -> None:
        if self.state.sequential:
            async with self.queue_lock:
                await self._handle(body)
        else:
            await self._handle(body)

    async def _handle(self, body: bytes) -> None:
        try:
            request = msgpack.unpackb(body, raw=False)
        except Exception as e:
            self.send(reply("bad_msgpack", "error", stderr=f"Failed to unpack: {e}", exit_code=1))
            return

        self.state.requests.append(request)
        msg_id = request.get("id", "")
        action = request.get("action")
        payload = request.get("payload") or {}

        if action == "ssh_connect":
            await asyncio.sleep(self.state.connect_delay)
            self._connect(msg_id, payload.get("connection"))

        elif action == "ssh_exec":
            if msg_id not in self.sessions:
                self.send(reply(msg_id, "error", stderr=NO_SESSION, exit_code=1))
                return
            command = payload.get("command")
            if not command:
                self.send(
                    reply(msg_id, "error", stderr="Missing 'command' in payload", exit_code=1)
                )
                return
            if self.state.run_shell:
                stdout, stderr, code = await asyncio.to_thread(self._run_shell, command)
                if self.state.lose_exit_status:
                    code = 0
                self.send(reply(msg_id, "ok", stdout=stdout, stderr=stderr, exit_code=code))
            elif "__SYNEX_GPU__" in command:  # диски и GPU для мониторинга
                self.send(reply(msg_id, "ok", stdout=self.state.hardware_output))
            else:
                if command.startswith("sleep "):
                    await asyncio.sleep(float(command.split()[1]))
                self.send(reply(msg_id, "ok", stdout=f"ran: {command}\n"))

        elif action == "monitor_get_values":
            session = self.sessions.get(msg_id)
            if session is None:
                self.send(reply(msg_id, "error", stderr=NO_SESSION, exit_code=1))
                return
            self.send(self._monitor(msg_id, session))

        elif action == "ssh_disconnect":
            if self.sessions.pop(msg_id, None) is None:
                self.send(reply(msg_id, "error", stderr="Session not found", exit_code=1))
            else:
                self.send(reply(msg_id, "ok", stdout=f"Disconnected id: {msg_id}"))

        elif isinstance(action, str) and action.startswith("sftp_"):
            if msg_id not in self.sessions:
                self.send(reply(msg_id, "error", stderr=NO_SESSION, exit_code=1))
                return
            failure = self.state.sftp_fail.get(action)
            if failure is None and self.state.sftp_fail_if is not None:
                failure = self.state.sftp_fail_if(action, payload)
            if failure is not None:
                self.send(reply(msg_id, "error", stderr=failure, exit_code=1))
                return
            task = asyncio.current_task()
            if task is not None:
                self.sftp_tasks.add(task)
            try:
                self.send(await FakeSftp(self.state).handle(msg_id, action, payload))
            finally:
                self.sftp_tasks.discard(task)  # type: ignore[arg-type]

        elif action == "test_sleep":
            await asyncio.sleep(float(payload.get("seconds", 0)))
            self.send(reply(msg_id, "ok", stdout=str(payload.get("echo", ""))))

        elif action == "test_big":
            self.send(reply(msg_id, "ok", stdout="x" * int(payload["size"])))

        elif action == "test_close":
            if self.transport is not None:
                self.transport.close()

        elif action == "test_not_a_map":
            self.send(["not", "a", "map"])

        elif action == "test_crash":
            os._exit(3)

        else:
            self.send(reply(msg_id, "error", stderr=f"Unknown action: {action}", exit_code=1))

    def _connect(self, msg_id: str, connection: dict[str, Any] | None) -> None:
        if not connection:
            self.send(reply(msg_id, "error", stderr="Missing 'connection' in payload", exit_code=1))
            return

        key_path = connection.get("key_path")
        passphrase = connection.get("passphrase")
        password = connection.get("password")
        if self.state.log_to_stdout:
            print(
                LOG_LINE.format(
                    f"Попытка подключения к {connection.get('username')}@{connection.get('host')}:"
                    f"{connection.get('port')} (key: {key_path}, passphrase: {passphrase}, "
                    f"password: {password})..."
                ),
                flush=True,
            )

        key = None
        if key_path:
            path = Path(key_path)
            key = path.read_bytes() if path.is_file() else None
            self.state.connect_key_files.append(key)

        error: str | None = None
        if connection.get("host") == "unreachable":
            error = "Connection failed: Connection refused"
        elif key_path:
            if key is None:
                error = "Connection failed: Failed to load SSH key: No such file or directory"
            elif b"ENCRYPTED" in key and passphrase is None:
                error = "Connection failed: Failed to load SSH key: The key is encrypted"
            elif b"ENCRYPTED" in key and passphrase != PASSPHRASE:
                error = "Connection failed: Failed to load SSH key: SshKey: cryptographic error"
        elif password is not None:
            if password != PASSWORD:
                error = "Connection failed: SSH authentication failed: access denied"
        else:
            error = "Connection failed: SSH internal error: No authentication method provided"

        if error is not None:
            self.send(reply(msg_id, "error", stderr=error, exit_code=1))
            return
        if self.transport is None or self.transport.is_closing():
            return  # подключение уже закрыто: воркер закрыл и его сессии
        self.sessions[msg_id] = dict(connection)
        self.send(reply(msg_id, "ok", stdout=f"Connected successfully, id: {msg_id}"))

    def _monitor(self, msg_id: str, session: dict[str, Any]) -> dict[str, Any]:
        if session.get("host") == "no-proc":
            return reply(
                msg_id,
                "ok",
                stdout="Пустой ответ от сервера (команда не выполнилась)",
                stderr="bash: head: command not found",
                exit_code=127,
            )
        if self.state.monitor_error:
            values: dict[str, Any] = {"ok": False, "error": self.state.monitor_error}
        else:
            calls = session["monitor_calls"] = session.get("monitor_calls", 0) + 1
            used = 1_200_000 + calls * 1_000
            values = {
                "ok": True,
                "cpu_usage_percent": BOOT_CPU if calls == 1 else 8.0 + calls,
                "mem_total_kb": MEM_TOTAL_KB,
                "mem_available_kb": MEM_TOTAL_KB - used,
                "mem_used_kb": used,
                "mem_usage_percent": round(used / MEM_TOTAL_KB * 100, 2),
                "error": None,
            }
        return reply(msg_id, "ok", stdout=json.dumps(values, separators=(",", ":")))

    def _run_shell(self, command: str) -> tuple[str, str, int]:
        if self.state.shell is not None:
            return self.state.shell.run(command)
        env = dict(os.environ)
        env["SHELL"] = "/bin/sh"
        if self.state.home:
            env["HOME"] = self.state.home
        result = subprocess.run(
            ["sh", "-c", command],
            cwd=self.state.home,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout, result.stderr, result.returncode


INTERNAL = "SSH internal error: "
NO_SUCH_FILE = "No such file: No such file"
FAILURE = "Failure: Failure"
MB = 1024 * 1024


def _rights(mode: int) -> str:
    bits = "rwxrwxrwx"
    return "".join(c if mode & (1 << (8 - i)) else "-" for i, c in enumerate(bits))


class FakeSftp:
    """sftp_* настоящего воркера (synex_core/src/sftp.rs) на файлах этой машины."""

    def __init__(self, state: FakeWorkerState) -> None:
        self.state = state

    async def handle(self, msg_id: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        remote = payload.get("remote_path")
        local = payload.get("local_path")
        target = payload.get("destination_path")
        try:
            if action == "sftp_list_dir":
                return reply(msg_id, "ok", stdout=self.list_dir(remote))
            if action == "sftp_create_dir":
                self.mkdir(remote)
            elif action == "sftp_remove_dir":
                await _when_released(lambda: self.rmdir(remote))
            elif action == "sftp_remove_file":
                await _when_released(lambda: self.remove(remote))
            elif action == "sftp_move_file":
                if not self.move(remote, target):
                    return reply(msg_id, "error")  # воркер не передаёт текст ошибки
            elif action == "sftp_copy_file":
                self.copy(remote, target)
            elif action == "sftp_upload_file":
                await self.upload(local, remote)
            elif action == "sftp_download_file":
                await self.download(remote, local)
                return reply(msg_id, "success")
            else:
                return reply(msg_id, "error", stderr=f"Unknown action: {action}", exit_code=1)
        except _SftpError as e:
            return reply(msg_id, "error", stderr=INTERNAL + str(e), exit_code=1)
        except OSError:
            # Файл занят или имя недопустимо на этой машине (Windows): сервер SFTP
            # ответил бы статусом Failure, а не молчанием
            stderr = f"{INTERNAL}{action} failed: {FAILURE}"
            return reply(msg_id, "error", stderr=stderr, exit_code=1)
        return reply(msg_id, "ok")

    def list_dir(self, path: str | None) -> str:
        if path is None:
            raise _SftpError("Missing directory path in payload")
        directory = self.state.remote(path)
        if not directory.is_dir():
            raise _SftpError(f"Failed to read remote directory: {NO_SUCH_FILE}")
        entries = []
        for item in directory.iterdir():
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode):
                kind = "symlink"
            elif stat.S_ISDIR(info.st_mode):
                kind = "folder"
            else:
                kind = "file"
            entries.append(
                {
                    "Type": kind,
                    "name": item.name,
                    "weight": info.st_size,
                    "rights": _rights(stat.S_IMODE(info.st_mode)),
                }
            )
        return json.dumps(entries, ensure_ascii=False, separators=(",", ":"))

    def mkdir(self, path: str | None) -> None:
        target = self.state.remote(path or "")
        if target.exists() or target.is_symlink():
            raise _SftpError(f"Failed to create remote directory: {FAILURE}")
        if not target.parent.is_dir():
            raise _SftpError(f"Failed to create remote directory: {NO_SUCH_FILE}")
        target.mkdir()

    def rmdir(self, path: str | None) -> None:
        target = self.state.remote(path or "")
        if not target.exists():
            raise _SftpError(f"Failed to remove remote directory: {NO_SUCH_FILE}")
        if not target.is_dir() or target.is_symlink() or any(target.iterdir()):
            raise _SftpError(f"Failed to remove remote directory: {FAILURE}")
        target.rmdir()

    def remove(self, path: str | None) -> None:
        target = self.state.remote(path or "")
        if not target.exists() and not target.is_symlink():
            raise _SftpError(f"Failed to remove remote file: {NO_SUCH_FILE}")
        if target.is_dir() and not target.is_symlink():
            raise _SftpError(f"Failed to remove remote file: {FAILURE}")
        target.unlink()

    def move(self, source: str | None, target: str | None) -> bool:
        src, dst = self.state.remote(source or ""), self.state.remote(target or "")
        # SFTP rename сервера OpenSSH не заменяет существующий файл
        if not (src.exists() or src.is_symlink()) or dst.exists() or not dst.parent.is_dir():
            return False
        src.rename(dst)
        return True

    def copy(self, source: str | None, target: str | None) -> None:
        src, dst = self.state.remote(source or ""), self.state.remote(target or "")
        if not src.exists():
            raise _SftpError(
                f"Failed to copy remote file: cp: cannot stat '{source}': "
                "No such file or directory\n"
            )
        if src.is_dir():
            return  # cp без -r папку не копирует, а код возврата воркер теряет
        if dst.is_dir():
            dst = dst / src.name
        shutil.copyfile(src, dst)

    async def upload(self, local: str | None, remote: str | None) -> None:
        source = Path(local or "")
        if not source.is_file():
            raise _SftpError("Failed to open local file: No such file or directory (os error 2)")
        path = remote or ""
        target = self.state.remote(path)
        if path.endswith("/") or target.is_dir():
            target = target / source.name
        if not target.parent.is_dir():
            raise _SftpError(f"Failed to create remote file: {NO_SUCH_FILE}")
        if target.is_dir():
            raise _SftpError(f"Failed to create remote file: {FAILURE}")
        await self._copy_stream(source, target)

    async def download(self, remote: str | None, local: str | None) -> None:
        source = self.state.remote(remote or "")
        if not source.exists():
            raise _SftpError(f"Failed to open remote file '{remote}': {NO_SUCH_FILE}")
        path = local or ""
        target = Path(path)
        if path.endswith(("/", "\\")) or target.is_dir():
            target = target / source.name
        if not target.parent.is_dir():
            raise _SftpError("Failed to create local file: No such file or directory (os error 2)")
        if source.is_dir():
            target.write_bytes(b"")  # как воркер: локальный файл уже создан
            raise _SftpError("Failed to download file: Failure")
        await self._copy_stream(source, target)

    async def _copy_stream(self, source: Path, target: Path) -> None:
        with source.open("rb") as reader, target.open("wb") as writer:
            while chunk := reader.read(MB):
                writer.write(chunk)
                writer.flush()
                if self.state.sftp_delay:
                    await asyncio.sleep(self.state.sftp_delay)


class _SftpError(Exception):
    pass


async def _when_released(operation: Callable[[], None], wait: float = 2.0) -> None:
    """На Windows файл, который ещё держит прерванная передача, не удалить, пока она
    его не закроет: подождать немного, как сервер, у которого файл освобождается."""
    deadline = asyncio.get_running_loop().time() + wait
    while True:
        try:
            operation()
            return
        except PermissionError:
            if asyncio.get_running_loop().time() >= deadline:
                raise
            await asyncio.sleep(0.02)


async def _serve_forever() -> None:
    port = int(os.environ["RUST_IPC_TCP_FALLBACK_PORT"])
    state = FakeWorkerState(
        run_shell=os.environ.get("FAKE_WORKER_SHELL") == "1",
        home=os.environ.get("FAKE_WORKER_HOME"),
        log_to_stdout=True,
    )
    state.accept_once = os.environ.get("FAKE_WORKER_ACCEPT_ONCE") == "1"
    loop = asyncio.get_running_loop()

    def protocol() -> FakeWorkerProtocol:
        return FakeWorkerProtocol(state)

    socket_path = os.environ.get("RUST_IPC_SOCKET_PATH", "")
    if os.environ.get("FAKE_WORKER_TRANSPORT") == "unix" and socket_path:
        # Как UnixListener::bind в Rust: занятый путь (даже мёртвым сокетом) это ошибка,
        # тогда воркер уходит на TCP. asyncio сам удалил бы старый сокет, поэтому проверка.
        if os.path.exists(socket_path):
            print(f"Socket failed (Address in use: {socket_path}), falling back to TCP")
        else:
            state.server = await loop.create_unix_server(protocol, socket_path)
            print(f"🔌 Listening on unix: {socket_path}", flush=True)
    else:
        # Как настоящий воркер на Windows: сначала объявляет pipe, потом уходит на TCP
        pipe = os.environ.get("RUST_IPC_PIPE_NAME", r"\\.\pipe\synex_worker")
        print(f"🔌 Listening on pipe: {pipe}", flush=True)
        print("Pipe failed (fake worker), falling back to TCP", flush=True)
    if state.server is None:
        state.server = await loop.create_server(protocol, "127.0.0.1", port)
        print(f"🔌 Listening on tcp 127.0.0.1:{port}", flush=True)

    spam = int(os.environ.get("FAKE_WORKER_SPAM", "0"))
    if spam:
        # Если родитель не читает stdout/stderr, эти записи заблокируются, event loop
        # встанет и воркер перестанет отвечать. Ровно так вёл себя старый менеджер.
        line = "worker log line " + "." * 60 + "\n"
        for _ in range(spam // len(line) + 1):
            sys.stdout.write(line)
            sys.stderr.write(line)
        sys.stdout.flush()
        sys.stderr.flush()

    await asyncio.Event().wait()  # работаем, пока процесс не убьют


if __name__ == "__main__":
    # Эмодзи в stdout: на Windows без этого print упал бы на кодировке cp1251
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    asyncio.run(_serve_forever())
