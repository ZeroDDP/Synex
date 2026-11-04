"""Общая обвязка тестов плагина ssh: приложение со своим менеджером сессий, подключённым
к фейковому воркеру, и запуск его на настоящем uvicorn (для WebSocket)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI
from websockets.asyncio.client import ClientConnection

from app.api.router import build_api_router
from app.core.errors import register_error_handlers
from app.plugins.manager import PluginManager
from app.plugins.ssh.manager import SSHSessionManager
from app.plugins.ssh.routes import get_ssh_manager
from tests.conftest import FakeWorkerProvider, get_free_port


class Harness:
    """Плагин ssh со своим менеджером сессий, подключённым к фейковому воркеру."""

    def __init__(self, app: FastAPI, provider: FakeWorkerProvider, manager: SSHSessionManager):
        self.app = app
        self.provider = provider
        self.manager = manager
        self.api: httpx.AsyncClient | None = None

    async def disconnect(self) -> None:
        await self.manager.close_all(reason="test")
        await self.provider.close()


def build_app(manager: SSHSessionManager) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    api_router = build_api_router()
    PluginManager().discover(api_router, enabled=["ssh"])
    app.include_router(api_router)
    app.dependency_overrides[get_ssh_manager] = lambda: manager
    return app


async def make_api(
    port: int, missing_socket_path: str, request_timeout: float = 5, channels: bool = True
) -> tuple[httpx.AsyncClient, Harness]:
    provider = FakeWorkerProvider(
        port, missing_socket_path, request_timeout=request_timeout, channels=channels
    )
    await provider.start()
    manager = SSHSessionManager(provider)
    app = build_app(manager)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return client, Harness(app, provider, manager)


@asynccontextmanager
async def serve(app: FastAPI) -> AsyncIterator[str]:
    """Приложение на настоящем uvicorn. Отдаёт базовый адрес ws://127.0.0.1:PORT."""
    port = get_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(15):
            while not server.started:
                if task.done():
                    raise RuntimeError("uvicorn failed to start")
                await asyncio.sleep(0.02)
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        async with asyncio.timeout(15):
            await task


async def recv_json(ws: ClientConnection, timeout: float = 5.0) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


async def send_json(ws: ClientConnection, message_type: str, data=None) -> None:
    await ws.send(json.dumps({"type": message_type, "data": data}))
