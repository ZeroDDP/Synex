"""Работа с Rust-ядром synex_worker (см. CoreDocs).

WorkerManager запускает бинарник как дочерний процесс и следит за ним,
WorkerClient отправляет action'ы по IPC (Named Pipe / Unix socket / TCP).
"""

from app.worker.client import WorkerClient
from app.worker.manager import WORKER_STATUS_EVENT, WorkerManager, worker_manager

__all__ = ["WORKER_STATUS_EVENT", "WorkerClient", "WorkerManager", "worker_manager"]
