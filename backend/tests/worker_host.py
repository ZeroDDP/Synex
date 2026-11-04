"""Отдельный процесс «backend» с WorkerManager и фейковым воркером.

Для тестов, где backend убивают через kill -9 (tests/test_worker_process.py): печатает
"WORKER_PID <pid>" и работает, пока его не убьют. Настройки берёт из окружения
(RUST_IPC_SOCKET_PATH, RUST_IPC_TCP_FALLBACK_PORT, FAKE_WORKER_TRANSPORT).

--no-pdeathsig: запуск без PR_SET_PDEATHSIG, как на macOS, где его нет.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.worker.manager as manager_module  # noqa: E402
from app.worker.manager import WorkerManager  # noqa: E402

FAKE_WORKER = Path(__file__).with_name("fake_worker.py")


async def main() -> None:
    if "--no-pdeathsig" in sys.argv:
        manager_module.popen_options = lambda: {"start_new_session": True}
    manager = WorkerManager()
    manager._worker_command = lambda: [sys.executable, str(FAKE_WORKER)]  # type: ignore[method-assign]
    await manager.start()
    print(f"WORKER_PID {manager.status()['worker_pid']}", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
