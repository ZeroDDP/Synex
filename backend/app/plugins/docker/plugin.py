from __future__ import annotations

from app.plugins.base import BasePlugin
from app.plugins.docker.routes import router


class DockerPlugin(BasePlugin):
    """Docker на серверах: /api/docker/sessions/{session_id}/... Команды docker CLI
    выполняются в SSH-сессии служебными командами и в терминал не попадают."""

    name = "docker"
    version = "0.1.0"
    description = "Контейнеры Docker на серверах"
    requires = ("ssh",)
    router = router
