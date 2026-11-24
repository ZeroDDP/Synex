from __future__ import annotations

from app.plugins.base import BasePlugin
from app.plugins.docker.logs import close_streams
from app.plugins.docker.routes import router


class DockerPlugin(BasePlugin):
    """HTTP and WebSocket interface to Docker operations in the Rust worker."""

    name = "docker"
    version = "0.2.0"
    description = "Контейнеры Docker на серверах"
    requires = ("ssh",)
    router = router

    async def on_unload(self) -> None:
        await close_streams()
