from __future__ import annotations

from typing import Any

import structlog

from app.core.event_bus import event_bus
from app.plugins.base import BasePlugin
from app.plugins.ssh.routes import router
from app.plugins.ssh.session import ssh_sessions
from app.worker import WORKER_STATUS_EVENT
from app.worker.manager import STATUS_CONNECTION_LOST, STATUS_WORKER_EXITED

logger = structlog.get_logger()

# Сессии принадлежат IPC-подключению: при его обрыве воркер закрывает их сам.
SESSIONS_LOST_ON = frozenset({STATUS_CONNECTION_LOST, STATUS_WORKER_EXITED})


async def on_worker_status(event: dict[str, Any]) -> None:
    payload = event.get("payload") or {}
    status = payload.get("status")
    if status in SESSIONS_LOST_ON and len(ssh_sessions):
        logger.warning("Rust worker connection is gone, SSH sessions are lost", status=status)
        ssh_sessions.clear(reason=str(status))


class SSHPlugin(BasePlugin):
    """SSH-сессии через synex_worker: /api/ssh/connect, exec, disconnect, status."""

    name = "ssh"
    version = "0.4.0"
    router = router

    async def on_load(self) -> None:
        event_bus.subscribe(WORKER_STATUS_EVENT, on_worker_status)

    async def on_unload(self) -> None:
        event_bus.unsubscribe(WORKER_STATUS_EVENT, on_worker_status)
        ssh_sessions.clear(reason="backend shutdown")
