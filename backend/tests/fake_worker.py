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
    перехода с pipe на TCP.

SSH-сервер воображаемый:
  * host "unreachable": "Connection failed: Connection refused";
  * password: подходит только PASSWORD;
  * key_path: файл должен существовать; если в нём есть "ENCRYPTED", нужна парольная
    фраза PASSPHRASE.

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
import subprocess
import sys
from pathlib import Path
from typing import Any

import msgpack

PASSWORD = "right-password"
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


class FakeWorkerState:
    def __init__(
        self,
        *,
        run_shell: bool = False,
        home: str | None = None,
        lose_exit_status: bool = True,
        sequential: bool = True,
        log_to_stdout: bool = False,
        connect_delay: float = 0.0,
    ) -> None:
        self.run_shell = run_shell
        self.connect_delay = connect_delay
        self.sequential = sequential
        self.lose_exit_status = lose_exit_status
        self.home = home
        self.log_to_stdout = log_to_stdout
        # Содержимое файла ключа на момент ssh_connect (None, если файла нет)
        self.connect_key_files: list[bytes | None] = []
        self.requests: list[dict[str, Any]] = []
        self.protocols: list[FakeWorkerProtocol] = []
        self.port: int | None = None
        self.server: asyncio.Server | None = None
        self.accept_once = False

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
        self.queue_lock = asyncio.Lock()
        self.sessions: dict[str, dict[str, Any]] = {}
        state.protocols.append(self)

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport
        if self.state.accept_once and self.state.server is not None:
            self.state.server.close()  # новые соединения больше не принимаются

    def connection_lost(self, exc: Exception | None) -> None:
        self.sessions.clear()  # авто-закрытие сессий после отключения Python

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
            else:
                if command.startswith("sleep "):
                    await asyncio.sleep(float(command.split()[1]))
                self.send(reply(msg_id, "ok", stdout=f"ran: {command}\n"))

        elif action == "monitor_get_values":
            if msg_id not in self.sessions:
                self.send(reply(msg_id, "error", stderr=NO_SESSION, exit_code=1))
                return
            values = {
                "ok": True,
                "cpu_usage_percent": 2.63,
                "mem_total_kb": 2014404,
                "mem_available_kb": 793976,
                "mem_used_kb": 1220428,
                "mem_usage_percent": 60.59,
                "error": None,
            }
            self.send(reply(msg_id, "ok", stdout=json.dumps(values, separators=(",", ":"))))

        elif action == "ssh_disconnect":
            if self.sessions.pop(msg_id, None) is None:
                self.send(reply(msg_id, "error", stderr="Session not found", exit_code=1))
            else:
                self.send(reply(msg_id, "ok", stdout=f"Disconnected id: {msg_id}"))

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

    def _run_shell(self, command: str) -> tuple[str, str, int]:
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


async def _serve_forever() -> None:
    port = int(os.environ["RUST_IPC_TCP_FALLBACK_PORT"])
    state = FakeWorkerState(
        run_shell=os.environ.get("FAKE_WORKER_SHELL") == "1",
        home=os.environ.get("FAKE_WORKER_HOME"),
        log_to_stdout=True,
    )
    state.accept_once = os.environ.get("FAKE_WORKER_ACCEPT_ONCE") == "1"
    loop = asyncio.get_running_loop()
    server = await loop.create_server(lambda: FakeWorkerProtocol(state), "127.0.0.1", port)
    state.server = server
    # Как настоящий воркер: сначала объявляет pipe, потом уходит на TCP
    pipe = os.environ.get("RUST_IPC_PIPE_NAME", r"\\.\pipe\synex_worker")
    print(f"🔌 Listening on pipe: {pipe}", flush=True)
    print("Pipe failed (fake worker), falling back to TCP", flush=True)
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
