from __future__ import annotations

from app.core.event_bus import event_bus
from app.plugins.base import BasePlugin
from app.plugins.sftp import service
from app.plugins.sftp.routes import router
from app.plugins.sftp.transfers import TRANSFER_EVENT
from app.plugins.ssh.manager import SSH_SESSION_EVENT


class SftpPlugin(BasePlugin):
    """Файлы на серверах: /api/sftp/sessions/{session_id}/... (список, операции,
    загрузка и скачивание), /api/sftp/transfers (передачи и прогресс), /api/sftp/local
    (папки этого компьютера). Работает через SFTP Rust-воркера."""

    name = "sftp"
    version = "0.1.0"
    description = "Файловый менеджер: файлы на серверах и передача с прогрессом"
    requires = ("ssh",)
    events = (TRANSFER_EVENT,)  # прогресс передач в /ws
    router = router

    async def on_load(self) -> None:
        event_bus.subscribe(SSH_SESSION_EVENT, self._on_session)

    async def on_unload(self) -> None:
        event_bus.unsubscribe(SSH_SESSION_EVENT, self._on_session)
        await service.shutdown()

    @staticmethod
    async def _on_session(event: dict) -> None:
        await service.get_sftp().on_session_event(event)
