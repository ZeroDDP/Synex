"""Настоящий synex_worker для тестов: где взять и как настроить backend под него.

Откуда берётся воркер (первое, что есть):
  1. SYNEX_TEST_WORKER: команда запуска (например, скрипт, который запускает exe через
     wine). Pipe тогда заведомо не создаётся, и воркер сразу уходит на TCP.
  2. Сборка для этой ОС по обычным правилам поиска (app/worker/executable.py):
     synex_worker.exe на Windows, synex_worker под Linux/macOS.

Используют tests/test_real_worker.py и интеграционные тесты tests/integration/.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.core.config import settings
from app.worker import manager as manager_module
from app.worker.executable import ensure_executable, find_executable
from tests.conftest import get_free_port, unique_pipe_name

WORKER_COMMAND = os.environ.get("SYNEX_TEST_WORKER")
_FOUND = find_executable(
    settings.rust_executable_path,
    settings.BASE_DIR,
    include_core=settings.rust_executable_path_is_default,
)
NATIVE_WORKER = None if WORKER_COMMAND else _FOUND.path
AVAILABLE = bool(WORKER_COMMAND or NATIVE_WORKER)
SKIP_REASON = (
    "needs synex_worker: a build for this OS in app/core/target/release "
    "(Windows: synex_worker.exe) or a launch command in SYNEX_TEST_WORKER"
)

# Каким транспортом backend подключится к воркеру
EXPECTED_TRANSPORT = "tcp" if WORKER_COMMAND else ("pipe" if sys.platform == "win32" else "unix")


def worker_command() -> list[str]:
    if WORKER_COMMAND:
        return [WORKER_COMMAND]
    assert NATIVE_WORKER is not None, SKIP_REASON
    # Как при обычном запуске: из git на Windows бинарник приходит без права на запуск
    assert ensure_executable(NATIVE_WORKER), f"cannot chmod +x {NATIVE_WORKER}"
    return [str(NATIVE_WORKER)]


@contextmanager
def real_worker_settings(monkeypatch) -> Iterator[None]:
    """Свои pipe, сокет и порт, чтобы не столкнуться с запущенным dev-бэкендом."""
    socket_dir = Path(tempfile.mkdtemp(prefix="sx"))  # короткий путь: лимит macOS
    pipe = "no-pipe-use-tcp" if WORKER_COMMAND else unique_pipe_name()
    monkeypatch.setattr(settings, "RUST_IPC_PIPE_NAME", pipe)
    monkeypatch.setattr(settings, "RUST_IPC_SOCKET_PATH", str(socket_dir / "worker.sock"))
    monkeypatch.setattr(settings, "RUST_IPC_TCP_FALLBACK_PORT", get_free_port())
    monkeypatch.setattr(settings, "RUST_IPC_REQUEST_TIMEOUT", 10.0)
    monkeypatch.setattr(settings, "RUST_IPC_CONNECT_TIMEOUT", 30.0)  # wine стартует долго
    monkeypatch.setattr(
        manager_module.WorkerManager, "_worker_command", lambda self: worker_command()
    )
    try:
        yield
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)
