"""Плагин sftp целиком: файлы на сервере (RemoteFS) и передачи (TransferManager) поверх
менеджера SSH-сессий."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.plugins.sftp.remote import RemoteFS
from app.plugins.sftp.transfers import TransferManager
from app.plugins.ssh.manager import ssh_manager


@dataclass
class SftpService:
    remote: RemoteFS
    transfers: TransferManager

    async def on_session_event(self, event: dict[str, Any]) -> None:
        """Сессию закрыли: её передачи прерываются, запомненное о сервере забывается."""
        if event.get("action") != "closed":
            return
        session_id = str(event.get("session_id"))
        self.remote.forget(session_id)
        for transfer in self.transfers.select(session_id=session_id, active=True):
            transfer.cancel_requested = True
            if transfer.status == "queued" and transfer.task is not None:
                transfer.task.cancel()


_service: SftpService | None = None


def get_sftp() -> SftpService:
    global _service
    if _service is None:
        remote = RemoteFS(ssh_manager)
        _service = SftpService(remote, TransferManager(remote))
    return _service


async def shutdown() -> None:
    global _service
    if _service is not None:
        await _service.transfers.shutdown()
        _service = None
