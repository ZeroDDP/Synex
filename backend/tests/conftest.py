from __future__ import annotations

import asyncio
import socket
import sys
import uuid
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.core.config import settings
from app.core.errors import WorkerConnectionError
from app.worker.client import WorkerClient
from tests.fake_worker import FakeWorkerProtocol, FakeWorkerState

FAKE_WORKER_SCRIPT = Path(__file__).with_name("fake_worker.py")


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def unique_pipe_name() -> str:
    return rf"\\.\pipe\synex_test_{uuid.uuid4().hex}"


@pytest.fixture(autouse=True)
def default_app_settings(monkeypatch):
    """Тесты не зависят от backend/.env разработчика: набор плагинов и источник метрик
    всегда как по умолчанию (тест может переопределить)."""
    monkeypatch.setattr(settings, "PLUGINS_ENABLED", "ssh,keys,monitor")
    monkeypatch.setattr(settings, "WS_FAKE_METRICS", False)


@pytest.fixture(autouse=True)
def isolated_keystore(monkeypatch, tmp_path: Path) -> Path:
    """Тесты никогда не трогают настоящее хранилище ключей и системный keychain."""
    path = tmp_path / "keystore" / "keys.json"
    monkeypatch.setattr(settings, "KEYSTORE_PATH", path)
    monkeypatch.setattr(settings, "KEYSTORE_MASTER_KEY", SecretStr(Fernet.generate_key().decode()))
    return path


@pytest.fixture
def free_port() -> int:
    return get_free_port()


@pytest.fixture
def missing_socket_path(tmp_path: Path) -> str:
    return str(tmp_path / "no-such-worker.sock")


async def start_fake_worker(state: FakeWorkerState) -> asyncio.Server:
    loop = asyncio.get_running_loop()
    port = get_free_port()
    server = await loop.create_server(lambda: FakeWorkerProtocol(state), "127.0.0.1", port)
    state.port = port
    return server


@pytest.fixture
async def fake_worker():
    state = FakeWorkerState()
    server = await start_fake_worker(state)
    yield state
    state.close_all()
    server.close()
    await server.wait_closed()


@pytest.fixture
async def shell_worker(tmp_path: Path):
    """Фейковый воркер, который реально выполняет команды через `sh -c`."""
    if sys.platform == "win32":
        pytest.skip("needs a POSIX shell")
    home = tmp_path / "home"
    home.mkdir()
    state = FakeWorkerState(run_shell=True, home=str(home))
    server = await start_fake_worker(state)
    yield state
    state.close_all()
    server.close()
    await server.wait_closed()


@pytest.fixture
def worker_settings(monkeypatch, free_port, missing_socket_path):
    monkeypatch.setattr(settings, "RUST_IPC_TCP_FALLBACK_PORT", free_port)
    monkeypatch.setattr(settings, "RUST_IPC_PIPE_NAME", unique_pipe_name())
    monkeypatch.setattr(settings, "RUST_IPC_SOCKET_PATH", missing_socket_path)
    monkeypatch.setattr(settings, "RUST_IPC_CONNECT_TIMEOUT", 10.0)
    monkeypatch.setattr(settings, "RUST_IPC_REQUEST_TIMEOUT", 5.0)
    monkeypatch.setattr(settings, "RUST_WORKER_AUTORESTART", True)
    return settings


class FakeWorkerProvider:
    """Подключения к фейковому воркеру для SSHSessionManager, как у WorkerManager.

    channels=False: воркер «не принимает» дополнительные подключения.
    """

    def __init__(
        self,
        port: int,
        socket_path: str,
        *,
        request_timeout: float = 5,
        channels: bool = True,
    ) -> None:
        self.port = port
        self.socket_path = socket_path
        self.request_timeout = request_timeout
        self.channels = channels
        self.main = self._client()
        self.opened: list[WorkerClient] = []

    def _client(self) -> WorkerClient:
        return WorkerClient(
            pipe_name=unique_pipe_name(),
            socket_path=self.socket_path,
            tcp_port=self.port,
            request_timeout=self.request_timeout,
        )

    async def start(self) -> None:
        await self.main.connect(timeout=3.0, prefer="tcp")

    def shared(self) -> WorkerClient:
        if not self.main.connected:
            raise WorkerConnectionError("Rust worker is not connected")
        return self.main

    async def open_channel(self) -> WorkerClient:
        if not self.channels or not self.main.connected:
            raise WorkerConnectionError("Cannot connect to Rust worker (test)")
        client = self._client()
        await client.connect(timeout=3.0, prefer="tcp")
        self.opened.append(client)
        return client

    @property
    def open_channels(self) -> int:
        return sum(client.connected for client in self.opened)

    async def close(self) -> None:
        for client in [*self.opened, self.main]:
            await client.disconnect()
