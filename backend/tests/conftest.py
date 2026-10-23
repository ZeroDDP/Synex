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
from tests.fake_worker import FakeWorkerProtocol, FakeWorkerState

FAKE_WORKER_SCRIPT = Path(__file__).with_name("fake_worker.py")


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def unique_pipe_name() -> str:
    return rf"\\.\pipe\synex_test_{uuid.uuid4().hex}"


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
