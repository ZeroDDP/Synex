"""Фикстуры интеграционных тестов: SSH-сервер, настоящий воркер и backend на uvicorn.

Тесты запускаются, когда есть:
  * воркер (tests/real_worker.py): сборка для этой ОС или команда в SYNEX_TEST_WORKER;
  * SSH-сервер: SYNEX_TEST_SSH_* или sshd на этой машине (Linux/macOS, см. sshd.py).
Иначе пропускаются с объяснением. Только они: `uv run pytest -m integration`;
без них: `uv run pytest -m "not integration"`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest

from app.core.app import create_app
from app.core.config import settings
from tests import real_worker as rw
from tests.integration.sshd import LocalSSHD, SSHTarget, external_target, find_sshd
from tests.integration.ui import UI, UI_ORIGIN
from tests.ssh_helpers import serve


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "/integration/" in item.nodeid.replace("\\", "/"):
            item.add_marker(pytest.mark.integration)
            if not rw.AVAILABLE:
                item.add_marker(pytest.mark.skip(reason=rw.SKIP_REASON))


@pytest.fixture(scope="session")
def ssh_target(tmp_path_factory) -> Iterator[SSHTarget]:
    external = external_target()
    if external is not None:
        yield external
        return
    if find_sshd() is None:
        pytest.skip("no SSH server: set SYNEX_TEST_SSH_* or install openssh-server")
    sshd = LocalSSHD(tmp_path_factory.mktemp("sshd"))
    try:
        target = sshd.start()
    except (RuntimeError, OSError) as e:
        pytest.skip(f"cannot start a local sshd: {e}")
    yield target
    sshd.stop()


@pytest.fixture
async def ui(monkeypatch, ssh_target: SSHTarget) -> AsyncIterator[UI]:
    """Backend целиком (воркер, плагины, /ws) на uvicorn и клиент-UI к нему. Ключ
    тестового сервера уже подтверждён, как после первого подключения в UI."""
    monkeypatch.setattr(settings, "ALLOWED_ORIGINS", UI_ORIGIN)
    with rw.real_worker_settings(monkeypatch):
        app = create_app()
        async with serve(app) as base:
            client = UI(base)
            try:
                await client.trust_server(ssh_target)
                yield client
            finally:
                await client.close()
