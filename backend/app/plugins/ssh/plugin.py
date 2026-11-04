from __future__ import annotations

from app.core.event_bus import event_bus
from app.plugins.base import BasePlugin
from app.plugins.ssh.manager import CLOSED_SHUTDOWN, ssh_manager
from app.plugins.ssh.routes import router
from app.worker import WORKER_STATUS_EVENT


class SSHPlugin(BasePlugin):
    """SSH-сессии через synex_worker: /api/ssh/sessions (REST и WebSocket терминала)
    и упрощённый API одной сессии /api/ssh/connect, exec, disconnect, status."""

    name = "ssh"
    version = "0.5.0"
    router = router

    async def on_load(self) -> None:
        event_bus.subscribe(WORKER_STATUS_EVENT, ssh_manager.on_worker_status)

    async def on_unload(self) -> None:
        event_bus.unsubscribe(WORKER_STATUS_EVENT, ssh_manager.on_worker_status)
        await ssh_manager.close_all(reason=CLOSED_SHUTDOWN)
