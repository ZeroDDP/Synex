from __future__ import annotations

from app.plugins.base import BasePlugin
from app.plugins.docker.logs import close_streams
from app.plugins.docker.routes import router


class DockerPlugin(BasePlugin):
    """Docker на серверах: /api/docker/sessions/{session_id}/... (контейнеры, действия,
    логи и живой лог по WebSocket, нагрузка, образы). Docker в Rust-ядре ещё нет, поэтому
    команды docker CLI выполняются в SSH-сессии служебными командами и в терминал не
    попадают."""

    name = "docker"
    version = "0.2.0"
    description = "Контейнеры Docker на серверах"
    requires = ("ssh",)
    router = router

    async def on_unload(self) -> None:
        await close_streams()
