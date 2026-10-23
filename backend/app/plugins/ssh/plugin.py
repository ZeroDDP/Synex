from __future__ import annotations

from typing import Any

import structlog

from app.core.event_bus import event_bus
from app.plugins.base import BasePlugin
from app.plugins.ssh.routes import router
from app.plugins.ssh.session import ssh_state
from app.worker import WORKER_STATUS_EVENT
from app.worker.manager import STATUS_WORKER_EXITED

logger = structlog.get_logger()


async def on_worker_status(event: dict[str, Any]) -> None:
    """Сессия живёт внутри процесса воркера: если он умер, сессии больше нет."""
    payload = event.get("payload") or {}
    if payload.get("status") == STATUS_WORKER_EXITED and ssh_state.is_connected:
        logger.warning("Rust worker exited, SSH session is lost", host=ssh_state.current_host)
        ssh_state.mark_disconnected()


class SSHPlugin(BasePlugin):
    """SSH-сессия через synex_worker: /api/ssh/connect, /api/ssh/exec, /api/ssh/status."""

    name = "ssh"
    version = "0.3.0"
    router = router

    async def on_load(self) -> None:
        event_bus.subscribe(WORKER_STATUS_EVENT, on_worker_status)

    async def on_unload(self) -> None:
        event_bus.unsubscribe(WORKER_STATUS_EVENT, on_worker_status)
        ssh_state.mark_disconnected()
