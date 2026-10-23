"""Фейковый synex_worker для тестов: тот же протокол, что в CoreDocs.

Используется двумя способами:
  * как asyncio.Protocol внутри теста (TCP, Unix socket или Named Pipe);
  * отдельным процессом: `python tests/fake_worker.py`. Слушает TCP 127.0.0.1 на
    RUST_IPC_TCP_FALLBACK_PORT и печатает "Listening on tcp ...", как настоящий
    воркер после fallback.

Поддерживаемые action:
  ssh_connect, ssh_exec                         как в CoreDocs (те же тексты ошибок)
  test_sleep {seconds, echo}                    ответ с задержкой, для параллельных запросов
  test_big {size}                               большой stdout
  test_close                                    воркер закрывает соединение без ответа
  test_not_a_map                                фрейм-список вместо словаря (как при to_vec)
  test_crash                                    процесс завершается с кодом 3

FAKE_WORKER_ACCEPT_ONCE=1: после первого клиента перестаёт принимать соединения
(имитирует воркер, который не умеет переподключение).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import msgpack


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
        lose_exit_status: bool = False,
        sequential: bool = False,
    ) -> None:
        self.connected = False
        self.run_shell = run_shell
        # Как текущий synex_worker: запросы обрабатываются строго по одному
        self.sequential = sequential
        self.queue_lock = asyncio.Lock()
        # Содержимое файла ключа на момент ssh_connect (None, если файла нет)
        self.connect_key_files: list[bytes | None] = []
        # Как текущий synex_worker: exit_code всегда 0, даже для `false`
        self.lose_exit_status = lose_exit_status
        self.home = home
        self.requests: list[dict[str, Any]] = []
        self.protocols: list[FakeWorkerProtocol] = []
        self.port: int | None = None
        self.server: asyncio.Server | None = None
        self.accept_once = False

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
        state.protocols.append(self)

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport
        if self.state.accept_once and self.state.server is not None:
            self.state.server.close()  # новые соединения больше не принимаются

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
            async with self.state.queue_lock:
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
            connection = payload.get("connection")
            if connection and connection.get("key_path"):
                key_file = Path(connection["key_path"])
                self.state.connect_key_files.append(
                    key_file.read_bytes() if key_file.is_file() else None
                )
            if not connection:
                self.send(
                    reply(msg_id, "error", stderr="Missing 'connection' in payload.", exit_code=1)
                )
            elif connection.get("host") == "unreachable":
                self.state.connected = False
                self.send(
                    reply(
                        msg_id,
                        "error",
                        stderr="Connection failed: Connection refused",
                        exit_code=1,
                    )
                )
            else:
                self.state.connected = True
                self.send(reply(msg_id, "ok", stdout="Connected successfully"))

        elif action == "ssh_exec":
            if not self.state.connected:
                self.send(
                    reply(
                        msg_id,
                        "error",
                        stderr="Not connected. Please send 'ssh_connect' first.",
                        exit_code=1,
                    )
                )
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
                self.send(reply(msg_id, "ok", stdout=f"ran: {command}"))

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
        # Воркер отдаёт stdout "без лишних \n в конце" (CoreDocs, 4.4)
        return result.stdout.rstrip("\n"), result.stderr.rstrip("\n"), result.returncode


async def _serve_forever() -> None:
    port = int(os.environ["RUST_IPC_TCP_FALLBACK_PORT"])
    state = FakeWorkerState(
        run_shell=os.environ.get("FAKE_WORKER_SHELL") == "1",
        home=os.environ.get("FAKE_WORKER_HOME"),
    )
    state.accept_once = os.environ.get("FAKE_WORKER_ACCEPT_ONCE") == "1"
    loop = asyncio.get_running_loop()
    server = await loop.create_server(lambda: FakeWorkerProtocol(state), "127.0.0.1", port)
    state.server = server
    print("Pipe failed (fake worker), falling back to TCP", flush=True)
    print(f"Listening on tcp 127.0.0.1:{port}", flush=True)

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
    asyncio.run(_serve_forever())
