from __future__ import annotations

from app.plugins.alerts.routes import router
from app.plugins.alerts.service import ALERT_EVENT, alert_service
from app.plugins.base import BasePlugin


class AlertsPlugin(BasePlugin):
    """Алерты всех серверов по метрикам плагина monitor: GET /api/alerts и событие
    alert в /ws."""

    name = "alerts"
    version = "0.1.0"
    description = "Алерты по метрикам всех серверов: CPU, память, диски, GPU, недоступность"
    requires = ("monitor",)
    events = (ALERT_EVENT,)
    router = router

    async def on_load(self) -> None:
        await alert_service.start()

    async def on_unload(self) -> None:
        await alert_service.stop()
