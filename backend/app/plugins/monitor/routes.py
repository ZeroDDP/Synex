from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.plugins.monitor.service import MetricsMonitor, metrics_monitor
from app.ws.metrics import MetricsSnapshot

router = APIRouter()


def get_monitor() -> MetricsMonitor:
    return metrics_monitor


Monitor = Annotated[MetricsMonitor, Depends(get_monitor)]


@router.get("/metrics", response_model=MetricsSnapshot, summary="Метрики серверов сейчас")
async def metrics(monitor: Monitor) -> MetricsSnapshot:
    """То же, что сообщение metrics в /ws. Пока этот эндпоинт вызывают (или к /ws
    подключён UI), серверы опрашиваются; первый вызов может вернуть status=waiting."""
    monitor.touch()
    return monitor.snapshot()
