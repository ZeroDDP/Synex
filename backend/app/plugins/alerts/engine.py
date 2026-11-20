"""Проверка метрик по правилам: когда алерт загорается, меняет уровень и гаснет.

Движок получает каждый снимок метрик (MetricsSnapshot, тот же, что уходит в /ws) и
сравнивает значения каждого сервера с правилами (rules.py):

* значение выше порога должно держаться duration_s секунд: короткий всплеск CPU
  алерта не даёт; у disk по умолчанию 0, то есть сразу;
* уровень critical важнее warning: если выполняются оба, алерт critical;
* алерт гаснет (resolved), когда значение опустилось ниже порога warning на HYSTERESIS
  (иначе значение около порога зажигало бы и гасило алерт каждые полсекунды). Ниже
  порога critical на HYSTERESIS critical сменяется на warning;
* server: сервер не отвечает. Опрос с ошибкой (status error) или свежих данных нет
  (stale), хотя сессии не заняты командами. Пока сервер не отвечает, остальные его
  метрики не проверяются: значения в них старые;
* сервер пропал из метрик (закрыли последнюю сессию): его алерты гаснут с причиной
  server_disconnected.

Ключ алерта: сервер, метрика и цель (раздел у disk, номер карты у gpu_temperature).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Literal

from app.plugins.alerts.rules import AlertRule, Metric
from app.ws.metrics import MetricsSnapshot, ServerMetrics

Severity = Literal["warning", "critical"]
Action = Literal["fired", "updated", "resolved", "acknowledged"]
HYSTERESIS = 5.0  # проценты или градусы
_RANK = {None: 0, "warning": 1, "critical": 2}

RESOLVED_NORMAL = "back_to_normal"
RESOLVED_DISCONNECTED = "server_disconnected"
RESOLVED_RULE_CHANGED = "rule_changed"

_LABELS: dict[Metric, str] = {
    "cpu": "CPU load",
    "memory": "Memory usage",
    "disk": "Disk usage",
    "gpu_temperature": "GPU temperature",
    "server": "Server",
}
_UNITS: dict[Metric, str] = {
    "cpu": "%",
    "memory": "%",
    "disk": "%",
    "gpu_temperature": "°C",
    "server": "",
}


@dataclass
class Alert:
    id: str
    server: str
    host: str
    port: int
    metric: Metric
    target: str  # раздел (disk), номер карты (gpu_temperature) или ""
    severity: Severity
    value: float | None
    peak: float | None
    threshold: float | None
    message: str
    started_at: int  # мс Unix
    updated_at: int
    status: Literal["active", "resolved"] = "active"
    resolved_at: int | None = None
    resolved_reason: str | None = None
    acknowledged: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "server": self.server,
            "host": self.host,
            "port": self.port,
            "metric": self.metric,
            "target": self.target,
            "severity": self.severity,
            "status": self.status,
            "value": self.value,
            "peak": self.peak,
            "threshold": self.threshold,
            "message": self.message,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "resolved_at": self.resolved_at,
            "resolved_reason": self.resolved_reason,
            "acknowledged": self.acknowledged,
        }


@dataclass
class Change:
    action: Action
    alert: Alert


Key = tuple[str, Metric, str]


@dataclass
class _Track:
    """Сколько значение держится выше порогов (монотонное время, сек)."""

    above_warning: float | None = None
    above_critical: float | None = None


@dataclass
class AlertEngine:
    rules: list[AlertRule]
    active: dict[Key, Alert] = field(default_factory=dict)
    _tracks: dict[Key, _Track] = field(default_factory=dict)

    def set_rules(self, rules: list[AlertRule], now_ms: int) -> list[Change]:
        """Новые правила. Алерты по выключенным правилам гаснут, отсчёт времени
        начинается заново."""
        self.rules = rules
        self._tracks.clear()
        enabled = {rule.metric for rule in rules if rule.enabled}
        changes = []
        for key, alert in list(self.active.items()):
            if alert.metric not in enabled:
                changes.append(self._resolve(key, now_ms, RESOLVED_RULE_CHANGED))
        return changes

    def evaluate(self, snapshot: MetricsSnapshot, now: float, now_ms: int) -> list[Change]:
        """Проверить снимок. now: монотонное время (сек), now_ms: Unix-время (мс)."""
        rules = {rule.metric: rule for rule in self.rules if rule.enabled}
        changes: list[Change] = []
        seen: set[Key] = set()
        present = {server.server for server in snapshot.servers}
        for server in snapshot.servers:
            for key, value, rule in self._values(server, rules):
                seen.add(key)
                change = self._check(key, server, value, rule, now, now_ms)
                if change is not None:
                    changes.append(change)
        for key, alert in list(self.active.items()):
            if alert.server not in present:
                changes.append(self._resolve(key, now_ms, RESOLVED_DISCONNECTED))
            elif key not in seen and _down(alert.server, snapshot) is False:
                # Цель пропала (раздел отмонтирован, карта исчезла) при живом сервере
                changes.append(self._resolve(key, now_ms, RESOLVED_NORMAL))
        for key in list(self._tracks):
            if key not in seen:
                del self._tracks[key]
        return changes

    # ----------------------------------------------------------------- values

    def _values(
        self, server: ServerMetrics, rules: dict[Metric, AlertRule]
    ) -> list[tuple[Key, float | None, AlertRule]]:
        """(ключ, значение, правило) для проверки. Значение None у server: «не отвечает»
        как 1.0, «отвечает» как 0.0."""
        name = server.server
        values: list[tuple[Key, float | None, AlertRule]] = []
        down = server.status == "error" or (server.status == "stale" and not server.busy)
        if "server" in rules:
            values.append(((name, "server", ""), 1.0 if down else 0.0, rules["server"]))
        if down or server.status in ("waiting", "stale"):
            # Значений нет или они старые: их алерты не меняются (но и не гаснут)
            for key in self.active:
                if key[0] == name and key[1] != "server":
                    values.append((key, None, rules.get(key[1]) or _off(key[1])))
            return values
        if "cpu" in rules and server.cpu is not None and server.cpu.percent is not None:
            values.append(((name, "cpu", ""), server.cpu.percent, rules["cpu"]))
        if "memory" in rules and server.memory is not None:
            values.append(((name, "memory", ""), server.memory.percent, rules["memory"]))
        if "disk" in rules:
            disks = {disk.mount: disk.percent for disk in server.disks or []}
            if not disks and server.disk is not None:
                disks["/"] = server.disk.percent
            for mount, percent in disks.items():
                values.append(((name, "disk", mount), percent, rules["disk"]))
        if "gpu_temperature" in rules:
            for gpu in server.gpus or []:
                if gpu.temperature is not None:
                    values.append(
                        ((name, "gpu_temperature", str(gpu.index)), gpu.temperature,
                         rules["gpu_temperature"])
                    )  # fmt: skip
        return values

    # ------------------------------------------------------------------ check

    def _check(
        self,
        key: Key,
        server: ServerMetrics,
        value: float | None,
        rule: AlertRule,
        now: float,
        now_ms: int,
    ) -> Change | None:
        if value is None:
            return None  # данных нет: ничего не меняется
        track = self._tracks.setdefault(key, _Track())
        current = self.active.get(key)
        warning, critical = (
            (None, 0.5) if rule.metric == "server" else (rule.warning, rule.critical)
        )

        track.above_warning = _since(track.above_warning, warning, value, now)
        track.above_critical = _since(track.above_critical, critical, value, now)
        wanted: Severity | None = None
        if _held(track.above_critical, rule.duration_s, now):
            wanted = "critical"
        elif _held(track.above_warning, rule.duration_s, now):
            wanted = "warning"

        if current is None:
            if wanted is None:
                return None
            alert = Alert(
                id=secrets.token_hex(6),
                server=key[0],
                host=server.host,
                port=server.port,
                metric=key[1],
                target=key[2],
                severity=wanted,
                value=value,
                peak=value,
                threshold=critical if wanted == "critical" else warning,
                message="",
                started_at=now_ms,
                updated_at=now_ms,
            )
            alert.message = _message(alert, rule)
            self.active[key] = alert
            return Change("fired", alert)

        current.value = value
        if current.peak is None or value > current.peak:
            current.peak = value
        if _RANK[wanted] > _RANK[current.severity]:
            return self._set_severity(current, wanted, critical, rule, now_ms)  # type: ignore[arg-type]
        lowest = warning if warning is not None else critical
        if lowest is not None and value < lowest - _hysteresis(rule):
            return self._resolve(key, now_ms, RESOLVED_NORMAL)
        if (
            current.severity == "critical"
            and warning is not None
            and critical is not None
            and value < critical - _hysteresis(rule)
        ):
            return self._set_severity(current, "warning", warning, rule, now_ms)
        return None

    def _set_severity(
        self, alert: Alert, severity: Severity, threshold: float | None, rule: AlertRule,
        now_ms: int,
    ) -> Change:  # fmt: skip
        alert.severity = severity
        alert.threshold = threshold
        alert.updated_at = now_ms
        alert.acknowledged = False  # стало хуже или лучше: пользователь должен увидеть
        alert.message = _message(alert, rule)
        return Change("updated", alert)

    def _resolve(self, key: Key, now_ms: int, reason: str) -> Change:
        alert = self.active.pop(key)
        self._tracks.pop(key, None)
        alert.status = "resolved"
        alert.resolved_at = now_ms
        alert.updated_at = now_ms
        alert.resolved_reason = reason
        return Change("resolved", alert)


def _since(since: float | None, threshold: float | None, value: float, now: float) -> float | None:
    if threshold is None or value < threshold:
        return None
    return since if since is not None else now


def _held(since: float | None, duration: float, now: float) -> bool:
    return since is not None and now - since >= duration


def _hysteresis(rule: AlertRule) -> float:
    return 0.0 if rule.metric == "server" else HYSTERESIS


def _down(server_name: str, snapshot: MetricsSnapshot) -> bool | None:
    for server in snapshot.servers:
        if server.server == server_name:
            return server.status == "error" or (server.status == "stale" and not server.busy)
    return None


def _off(metric: Metric) -> AlertRule:
    return AlertRule(metric=metric, warning=100, critical=100, enabled=False)


def _message(alert: Alert, rule: AlertRule) -> str:
    if alert.metric == "server":
        return f"Server {alert.server} does not respond for more than {rule.duration_s:g} s"
    label = _LABELS[alert.metric]
    unit = _UNITS[alert.metric]
    where = ""
    if alert.metric == "disk":
        where = f" of {alert.target}"
    elif alert.metric == "gpu_temperature":
        where = f" of GPU {alert.target}"
    held = f" for {rule.duration_s:g} s" if rule.duration_s else ""
    return (
        f"{label}{where} on {alert.server} is {alert.value:.0f}{unit}"
        f" (threshold {alert.threshold:g}{unit}){held}"
    )
