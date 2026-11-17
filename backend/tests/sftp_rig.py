"""Обвязка тестов плагина sftp: приложение с плагином, фейковый воркер с эмулятором
shell (его виртуальная ФС это «сервер»), открытая сессия и папка «этого компьютера»."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.core.config import settings
from app.plugins.sftp.remote import RemoteFS
from app.plugins.sftp.service import SftpService, get_sftp
from app.plugins.sftp.transfers import ACTIVE, TransferManager
from tests.fake_shell import HOME
from tests.plugin_rig import PluginRig, plugin_rig


@dataclass
class Files:
    rig: PluginRig
    service: SftpService
    events: list[dict]
    local: Path  # папка «этого компьютера»

    @property
    def base(self) -> str:
        return f"/api/sftp/sessions/{self.rig.session_id}"

    def server(self, path: str) -> Path:
        """Файл виртуальной ФС сервера (путь от корня или "~/...")."""
        if path == "~" or path.startswith("~/"):
            path = HOME + path[1:]
        return self.rig.worker.remote(path)

    def put(self, path: str, data: bytes = b"x") -> Path:
        file = self.server(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)
        return file

    def requests(self, action: str) -> list[dict]:
        return [r for r in self.rig.worker.requests if r["action"] == action]

    async def transfer(self, **body: object) -> dict:
        """Начать передачу по пути на этом компьютере и дождаться её конца."""
        started = await self.rig.api.post(f"{self.base}/transfers", json=body)
        assert started.status_code == 202, started.text
        return await self.finished(started.json()["id"])

    async def finished(self, transfer_id: str, timeout: float = 20) -> dict:
        async with asyncio.timeout(timeout):
            while True:
                info = (await self.rig.api.get(f"/api/sftp/transfers/{transfer_id}")).json()
                if info["status"] not in ACTIVE:
                    return info
                await asyncio.sleep(0.02)


@asynccontextmanager
async def files_rig(
    tmp_path: Path, socket_path: str, monkeypatch: pytest.MonkeyPatch, sftp_build: str
) -> AsyncIterator[Files]:
    """sftp_build: какую сборку воркера повторяет фейковый (см. FakeSftp)."""
    monkeypatch.setattr(settings, "SFTP_PROGRESS_INTERVAL", 0.02)
    events: list[dict] = []
    async with plugin_rig(tmp_path, socket_path, ["sftp"], {}) as rig:
        rig.worker.sftp_build = sftp_build  # type: ignore[assignment]

        async def publish(info: dict) -> None:
            events.append(info)

        remote = RemoteFS(rig.manager)
        sftp = SftpService(remote, TransferManager(remote, publish))
        rig.app.dependency_overrides[get_sftp] = lambda: sftp
        local = tmp_path / "local"
        local.mkdir()
        try:
            yield Files(rig, sftp, events, local)
        finally:
            await sftp.transfers.shutdown()
