"""Алерты всех серверов: движок (engine.py) на каждом снимке метрик, список активных и
история, события `alert` в /ws.

Снимки метрик приходят по шине (событие metrics): те же, что видит UI, в том числе
тестовые при WS_FAKE_METRICS. Плагин monitor опрашивает серверы, только пока метрики
кто-то смотрит; чтобы алерты работали и при закрытом Dashboard, сервис раз в несколько
секунд отмечается у монитора как читатель (ALERTS_KEEP_POLLING).
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

import structlog

from app.core.config import settings
from app.core.event_bus import event_bus
from app.core.periodic import Periodic
from app.plugins.alerts.engine import Alert, AlertEngine, Change
from app.plugins.alerts.errors import AlertActiveError, AlertNotFoundError
from app.plugins.alerts.rules import AlertRule, RulesStore, normalize
from app.ws.metrics import METRICS_EVENT, MetricsSnapshot

logger = structlog.get_logger()

ALERT_EVENT = "alert"


class AlertService:
    def __init__(self, store: RulesStore | None = None) -> None:
        self._store = store
        self.engine = AlertEngine(rules=[])
        self._history: OrderedDict[str, Alert] = OrderedDict()  # погасшие, новые в конце
        self._keeper: Periodic | None = None
        self.started = False

    @property
    def store(self) -> RulesStore:
        if self._store is None:
            return RulesStore(settings.alerts_path)
        return self._store

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self.started:
            return
        try:
            self.engine.rules = self.store.load()
        except Exception as e:
            # Испорченный файл не должен ронять плагин: правила по умолчанию, причина в логе
            logger.warning("Alert rules are not loaded, using defaults", error=str(e))
            self.engine.rules = normalize([])
        event_bus.subscribe(METRICS_EVENT, self.on_metrics)
        if settings.ALERTS_KEEP_POLLING:
            interval = max(1.0, settings.MONITOR_DEMAND_TIMEOUT / 2)
            self._keeper = Periodic("alerts-keep-polling", interval, self._keep_polling)
            self._keeper.start()
        self.started = True
        logger.info("Alerts started", rules=[r.metric for r in self.engine.rules if r.enabled])

    async def stop(self) -> None:
        event_bus.unsubscribe(METRICS_EVENT, self.on_metrics)
        if self._keeper is not None:
            await self._keeper.stop()
            self._keeper = None
        self.engine.active.clear()
        self._history.clear()
        self.started = False

    async def _keep_polling(self) -> None:
        from app.plugins.monitor.service import metrics_monitor

        metrics_monitor.touch()

    # ----------------------------------------------------------- evaluation

    async def on_metrics(self, snapshot: Any) -> None:
        if not isinstance(snapshot, MetricsSnapshot):
            snapshot = MetricsSnapshot.model_validate(snapshot)
        changes = self.engine.evaluate(snapshot, time.monotonic(), _now_ms())
        await self._apply(changes)

    async def _apply(self, changes: list[Change]) -> None:
        for change in changes:
            if change.action == "resolved":
                self._remember(change.alert)
            log = logger.warning if change.action == "fired" else logger.info
            log(
                "Alert " + change.action,
                server=change.alert.server,
                metric=change.alert.metric,
                target=change.alert.target or None,
                severity=change.alert.severity,
                value=change.alert.value,
            )
            await event_bus.publish(
                ALERT_EVENT, {"action": change.action, "alert": change.alert.as_dict()}
            )

    def _remember(self, alert: Alert) -> None:
        self._history[alert.id] = alert
        while len(self._history) > settings.ALERTS_HISTORY:
            self._history.popitem(last=False)

    # --------------------------------------------------------------- queries

    def alerts(self, *, status: str = "all", server: str | None = None) -> list[Alert]:
        """Активные (critical выше, новые выше), потом погасшие (новые выше)."""
        active = sorted(
            self.engine.active.values(),
            key=lambda a: (a.severity != "critical", -a.started_at),
        )
        resolved = sorted(self._history.values(), key=lambda a: -(a.resolved_at or 0))
        chosen = {"active": active, "resolved": resolved}.get(status, active + resolved)
        return [a for a in chosen if server is None or a.server == server]

    def get(self, alert_id: str) -> Alert:
        for alert in self.engine.active.values():
            if alert.id == alert_id:
                return alert
        alert = self._history.get(alert_id)
        if alert is None:
            raise AlertNotFoundError(f"Alert '{alert_id}' not found")
        return alert

    def counts(self) -> dict[str, int]:
        active = list(self.engine.active.values())
        return {
            "active": len(active),
            "critical": sum(1 for a in active if a.severity == "critical"),
            "warning": sum(1 for a in active if a.severity == "warning"),
            "unacknowledged": sum(1 for a in active if not a.acknowledged),
            "resolved": len(self._history),
        }

    # --------------------------------------------------------------- actions

    async def acknowledge(self, alert_id: str) -> Alert:
        alert = self.get(alert_id)
        if not alert.acknowledged:
            alert.acknowledged = True
            await event_bus.publish(
                ALERT_EVENT, {"action": "acknowledged", "alert": alert.as_dict()}
            )
        return alert

    async def acknowledge_all(self) -> int:
        count = 0
        for alert in list(self.engine.active.values()):
            if not alert.acknowledged:
                await self.acknowledge(alert.id)
                count += 1
        return count

    def dismiss(self, alert_id: str) -> None:
        alert = self.get(alert_id)
        if alert.status == "active":
            raise AlertActiveError(
                f"Alert '{alert_id}' is still active: it disappears by itself when the "
                "value is back to normal (acknowledge it to hide the badge)"
            )
        del self._history[alert_id]

    def clear_history(self) -> int:
        count = len(self._history)
        self._history.clear()
        return count

    def rules(self) -> list[AlertRule]:
        return list(self.engine.rules)

    async def set_rules(self, rules: list[AlertRule]) -> list[AlertRule]:
        rules = normalize(rules)
        self.store.save(rules)
        await self._apply(self.engine.set_rules(rules, _now_ms()))
        return rules

    async def reset_rules(self) -> list[AlertRule]:
        self.store.reset()
        rules = normalize([])
        await self._apply(self.engine.set_rules(rules, _now_ms()))
        return rules


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


alert_service = AlertService()
