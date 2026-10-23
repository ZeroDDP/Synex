from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from app.core.errors import WorkerConnectionError
from app.keystore import KeyStore, get_keystore
from app.worker import WorkerClient, worker_manager


def get_worker_client() -> WorkerClient:
    """FastAPI-зависимость: IPC-клиент к synex_worker."""
    client = worker_manager.client
    if client is None:
        raise WorkerConnectionError("Rust worker is not started")
    return client


Worker = Annotated[WorkerClient, Depends(get_worker_client)]
Keys = Annotated[KeyStore, Depends(get_keystore)]
