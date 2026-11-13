"""Обвязка тестов плагинов, которые работают через SSH-сессию (docker, git): приложение
с плагином и ssh, фейковый воркер с эмулятором shell и подставленными программами
сервера, открытая сессия."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import FastAPI

from app.api.router import build_api_router
from app.core.errors import register_error_handlers
from app.plugins.manager import PluginManager
from app.plugins.ssh.manager import SSHSessionManager
from app.plugins.ssh.routes import get_ssh_manager
from tests.conftest import FakeWorkerProvider, start_fake_worker
from tests.fake_server_tools import sftp_programs
from tests.fake_shell import Program
from tests.fake_worker import FakeWorkerState


@dataclass
class PluginRig:
    api: httpx.AsyncClient
    manager: SSHSessionManager
    worker: FakeWorkerState
    session_id: str
    app: FastAPI
    connect_body: dict

    @property
    def session_url(self) -> str:
        return f"sessions/{self.session_id}"


@asynccontextmanager
async def plugin_rig(
    tmp_path: Path,
    socket_path: str,
    plugins: list[str],
    programs: dict[str, Program] | None = None,
) -> AsyncIterator[PluginRig]:
    """programs: программы сервера для эмулятора shell (docker, git). None: команды
    выполняет настоящий `sh -c` с настоящими программами (только Linux/macOS)."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    emulate = programs is not None
    state = FakeWorkerState(run_shell=True, home=str(home), emulate_shell=emulate)
    if state.shell is not None:
        state.shell.programs.update(sftp_programs(state.shell))
        state.shell.programs.update(programs or {})
    server = await start_fake_worker(state)
    assert state.port is not None
    provider = FakeWorkerProvider(state.port, socket_path)
    await provider.start()
    manager = SSHSessionManager(provider)

    app = FastAPI()
    register_error_handlers(app)
    api_router = build_api_router()
    PluginManager().discover(api_router, enabled=plugins)
    app.include_router(api_router)
    app.dependency_overrides[get_ssh_manager] = lambda: manager

    key = tmp_path / "id_ed25519"
    key.write_text("key")
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
            body = {"host": "10.0.0.1", "username": "root", "key_path": str(key)}
            opened = await api.post("/api/ssh/sessions", json=body)
            assert opened.status_code == 201, opened.text
            yield PluginRig(api, manager, state, opened.json()["session_id"], app, body)
    finally:
        await manager.close_all(reason="test")
        await provider.close()
        state.close_all()
        server.close()
        await server.wait_closed()
