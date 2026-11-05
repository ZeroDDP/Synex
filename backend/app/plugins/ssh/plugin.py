from __future__ import annotations

from app.core.event_bus import event_bus
from app.plugins.base import BasePlugin
from app.plugins.ssh.manager import CLOSED_SHUTDOWN, SSH_SESSION_EVENT, ssh_manager
from app.plugins.ssh.routes import router
from app.worker import WORKER_STATUS_EVENT
from app.ws import ws_hub


class SSHPlugin(BasePlugin):
    """SSH-сессии через synex_worker: /api/ssh/sessions (REST и WebSocket терминала)
    и упрощённый API одной сессии /api/ssh/connect, exec, disconnect, status."""

    name = "ssh"
    version = "0.5.0"
    router = router

    async def on_load(self) -> None:
        event_bus.subscribe(WORKER_STATUS_EVENT, ssh_manager.on_worker_status)
        # Открытие и закрытие сессий (в том числе потерянных при падении воркера) в /ws:
        # UI обновляет список сессий, даже если терминал сессии не открыт
        ws_hub.forward(SSH_SESSION_EVENT)

    async def on_unload(self) -> None:
        event_bus.unsubscribe(WORKER_STATUS_EVENT, ssh_manager.on_worker_status)
        await ssh_manager.close_all(reason=CLOSED_SHUTDOWN)
