from __future__ import annotations

import structlog

from app.core.config import settings
from app.plugins.base import BasePlugin
from app.plugins.monitor.routes import router
from app.plugins.monitor.service import metrics_monitor

logger = structlog.get_logger()


class MonitorPlugin(BasePlugin):
    """Метрики серверов (CPU, RAM, сеть, диски, GPU) через synex_worker: событие metrics в
    /ws каждые 500 мс и GET /api/monitor/metrics. Серверы берутся из SSH-сессий плагина
    ssh."""

    name = "monitor"
    version = "0.2.0"
    description = "Метрики серверов: CPU, память, сеть, диски, видеокарты"
    requires = ("ssh",)
    router = router

    async def on_load(self) -> None:
        if settings.WS_FAKE_METRICS:
            logger.info("WS_FAKE_METRICS=true: /ws gets fake metrics, real ones go to REST only")
        metrics_monitor.start(publish=not settings.WS_FAKE_METRICS)

    async def on_unload(self) -> None:
        await metrics_monitor.stop()
