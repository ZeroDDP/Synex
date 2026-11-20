"""Алерты всех серверов (app/plugins/alerts): движок на снимках метрик, API и события."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.api.router import build_api_router
from app.core.config import settings
from app.core.errors import register_error_handlers
from app.core.event_bus import event_bus
from app.plugins.alerts.engine import RESOLVED_DISCONNECTED, RESOLVED_RULE_CHANGED, AlertEngine
from app.plugins.alerts.rules import DEFAULT_RULES, AlertRule, RulesStore, normalize
from app.plugins.alerts.service import ALERT_EVENT, AlertService, alert_service
from app.plugins.manager import PluginManager
from app.ws.metrics import (
    METRICS_EVENT,
    CpuMetrics,
    DiskMetrics,
    DiskUsage,
    GpuMetrics,
    MemoryMetrics,
    MetricsSnapshot,
    ServerMetrics,
)

GIB = 1024**3


def server(
    name: str = "10.0.0.1:22",
    *,
    cpu: float | None = 10,
    memory: float = 20,
    disks: dict[str, float] | None = None,
    gpu_temperature: float | None = None,
    status: str = "ok",
    busy: bool = False,
) -> ServerMetrics:
    host, port = name.rsplit(":", 1)
    return ServerMetrics(
        server=name,
        host=host,
        port=int(port),
        session_ids=["s1"],
        status=status,  # type: ignore[arg-type]
        busy=busy,
        cpu=CpuMetrics(percent=cpu),
        memory=MemoryMetrics(
            total=8 * GIB, used=int(8 * GIB * memory / 100), available=0, percent=memory
        ),
        disk=DiskUsage(total=100, used=50, available=50, percent=50),
        disks=[
            DiskMetrics(
                mount=mount,
                filesystem="/dev/vda1",
                total=100,
                used=int(p),
                available=0,
                percent=p,
            )  # fmt: skip
            for mount, p in (disks or {"/": 50}).items()
        ],
        gpus=[
            GpuMetrics(
                index=0,
                name="RTX",
                utilization=50,
                memory_used=None,
                memory_total=None,
                temperature=gpu_temperature,
            )  # fmt: skip
        ]
        if gpu_temperature is not None
        else [],
    )


def snapshot(*servers: ServerMetrics) -> MetricsSnapshot:
    return MetricsSnapshot(source="ssh", interval_ms=500, window_s=30, servers=list(servers))


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def ms(self) -> int:
        return int(self.now * 1000)


def run(engine: AlertEngine, clock: Clock, *servers: ServerMetrics, after: float = 0.5):
    clock.now += after
    return engine.evaluate(snapshot(*servers), clock.now, clock.ms())


# ------------------------------------------------------------------- engine


def test_cpu_must_stay_high_for_the_rule_duration():
    engine, clock = AlertEngine(rules=normalize([])), Clock()
    assert run(engine, clock, server(cpu=97)) == []
    assert run(engine, clock, server(cpu=97), after=30) == []  # 30 с из 60
    assert run(engine, clock, server(cpu=40), after=1) == []  # всплеск кончился
    assert run(engine, clock, server(cpu=97), after=1) == []
    (change,) = run(engine, clock, server(cpu=97), after=60)
    assert change.action == "fired"
    alert = change.alert
    assert (alert.metric, alert.severity, alert.threshold, alert.value) == (
        "cpu",
        "critical",
        95,
        97,
    )
    assert "CPU load on 10.0.0.1:22 is 97%" in alert.message
    assert alert.status == "active" and not alert.acknowledged


def test_warning_escalates_to_critical_and_back_with_hysteresis():
    rules = normalize([AlertRule(metric="memory", warning=80, critical=90, duration_s=0)])
    engine, clock = AlertEngine(rules=rules), Clock()
    (fired,) = run(engine, clock, server(memory=85))
    assert (fired.action, fired.alert.severity) == ("fired", "warning")
    (worse,) = run(engine, clock, server(memory=93))
    assert (worse.action, worse.alert.severity, worse.alert.threshold) == (
        "updated",
        "critical",
        90,
    )
    assert run(engine, clock, server(memory=88)) == []  # ниже 90, но не ниже 90-5
    (better,) = run(engine, clock, server(memory=84))
    assert (better.action, better.alert.severity) == ("updated", "warning")
    assert run(engine, clock, server(memory=78)) == []  # ниже 80, но не ниже 75
    (resolved,) = run(engine, clock, server(memory=70))
    assert (resolved.action, resolved.alert.status) == ("resolved", "resolved")
    assert resolved.alert.peak == 93
    assert resolved.alert.resolved_reason == "back_to_normal"
    assert engine.active == {}


def test_each_disk_and_gpu_has_its_own_alert():
    engine, clock = AlertEngine(rules=normalize([])), Clock()
    changes = run(engine, clock, server(disks={"/": 96, "/data": 92, "/boot": 30}))
    alerts = {c.alert.target: c.alert.severity for c in changes}
    assert alerts == {"/": "critical", "/data": "warning"}
    assert all("Disk usage of /" in c.alert.message for c in changes)
    # Раздел отмонтировали при живом сервере: его алерт гаснет
    (gone,) = run(engine, clock, server(disks={"/": 96}))
    assert (gone.alert.target, gone.action) == ("/data", "resolved")

    engine, clock = AlertEngine(rules=normalize([])), Clock()
    run(engine, clock, server(gpu_temperature=91))
    (hot,) = run(engine, clock, server(gpu_temperature=91), after=61)
    assert (hot.alert.metric, hot.alert.target, hot.alert.severity) == (
        "gpu_temperature",
        "0",
        "critical",
    )
    assert "GPU temperature of GPU 0" in hot.alert.message and "°C" in hot.alert.message


def test_server_that_does_not_respond():
    engine, clock = AlertEngine(rules=normalize([])), Clock()
    run(engine, clock, server(cpu=99))
    # Все сессии заняты командами: данных нет, но сервер жив
    assert run(engine, clock, server(status="stale", busy=True), after=40) == []
    assert run(engine, clock, server(status="error")) == []
    (down,) = run(engine, clock, server(status="error"), after=31)
    assert (down.alert.metric, down.alert.severity) == ("server", "critical")
    assert "does not respond" in down.alert.message
    # Пока сервер не отвечает, его старые CPU не зажигают алерт
    assert [c.alert.metric for c in run(engine, clock, server(status="stale"), after=100)] == []
    (back,) = run(engine, clock, server(cpu=10))
    assert (back.alert.metric, back.action) == ("server", "resolved")


def test_alerts_of_a_closed_server_resolve():
    engine, clock = AlertEngine(rules=normalize([])), Clock()
    other = server("10.0.0.2:22", disks={"/": 99})
    changes = run(engine, clock, server(disks={"/": 99}), other)
    assert {c.alert.server for c in changes} == {"10.0.0.1:22", "10.0.0.2:22"}
    (closed,) = run(engine, clock, other)
    assert (closed.alert.server, closed.alert.resolved_reason) == (
        "10.0.0.1:22",
        RESOLVED_DISCONNECTED,
    )


def test_disabled_rules_resolve_their_alerts():
    engine, clock = AlertEngine(rules=normalize([])), Clock()
    run(engine, clock, server(disks={"/": 99}))
    rules = normalize([AlertRule(metric="disk", warning=90, critical=95, enabled=False)])
    (change,) = engine.set_rules(rules, clock.ms())
    assert change.alert.resolved_reason == RESOLVED_RULE_CHANGED
    assert run(engine, clock, server(disks={"/": 99})) == []


def test_rules_validation_and_store(tmp_path: Path):
    with pytest.raises(ValueError, match="warning must not be above critical"):
        AlertRule(metric="cpu", warning=95, critical=90)
    with pytest.raises(ValueError, match="needs a warning or a critical"):
        AlertRule(metric="cpu")
    assert [r.metric for r in normalize([])] == [
        "cpu",
        "memory",
        "disk",
        "gpu_temperature",
        "server",
    ]

    store = RulesStore(tmp_path / "alerts.json")
    assert store.load() == list(DEFAULT_RULES)
    custom = normalize([AlertRule(metric="cpu", warning=50, critical=None, duration_s=5)])
    store.save(custom)
    assert store.load() == custom
    store.path.write_text("{broken", encoding="utf-8")
    with pytest.raises(Exception, match="damaged"):
        store.load()
    store.reset()
    assert store.load() == list(DEFAULT_RULES)


# ------------------------------------------------------------------- service and API


@pytest.fixture
async def alerts_api(monkeypatch):
    monkeypatch.setattr(settings, "ALERTS_KEEP_POLLING", False)
    app = FastAPI()
    register_error_handlers(app)
    router = build_api_router()
    plugins = PluginManager()
    plugins.discover(router, enabled=["alerts"])
    app.include_router(router)
    await plugins.load_all()
    events: list[dict] = []

    async def collect(data: dict) -> None:
        events.append(data)

    event_bus.subscribe(ALERT_EVENT, collect)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as api:
            yield api, events
    finally:
        event_bus.unsubscribe(ALERT_EVENT, collect)
        await plugins.unload_all()


async def test_alerts_from_metrics_events_and_api(alerts_api):
    api, events = alerts_api
    rules = await api.put(
        "/api/alerts/rules",
        json={"rules": [{"metric": "cpu", "warning": 80, "critical": 90, "duration_s": 0}]},
    )
    assert rules.status_code == 200, rules.text
    assert [r["metric"] for r in rules.json()["rules"]][0] == "cpu"
    assert settings.alerts_path.exists()

    await event_bus.publish(
        METRICS_EVENT, snapshot(server(cpu=85), server("10.0.0.2:22", disks={"/": 99}))
    )
    listed = (await api.get("/api/alerts")).json()
    assert listed["counts"] == {
        "active": 2, "critical": 1, "warning": 1, "unacknowledged": 2, "resolved": 0
    }  # fmt: skip
    first = listed["alerts"][0]
    assert (first["severity"], first["metric"], first["server"]) == (
        "critical",
        "disk",
        "10.0.0.2:22",
    )
    assert [e["action"] for e in events] == ["fired", "fired"]

    only = (await api.get("/api/alerts", params={"server": "10.0.0.1:22"})).json()["alerts"]
    assert [a["metric"] for a in only] == ["cpu"]
    acked = await api.post(f"/api/alerts/{only[0]['id']}/ack")
    assert acked.json()["acknowledged"] is True and events[-1]["action"] == "acknowledged"
    refused = await api.delete(f"/api/alerts/{only[0]['id']}")
    assert (refused.status_code, refused.json()["error"]) == (409, "alert_active")

    all_acked = await api.post("/api/alerts/ack-all")
    assert all_acked.json()["acknowledged"] == 1
    assert all_acked.json()["counts"]["unacknowledged"] == 0

    await event_bus.publish(METRICS_EVENT, snapshot(server(cpu=10)))
    listed = (await api.get("/api/alerts", params={"status": "resolved"})).json()
    reasons = {a["metric"]: a["resolved_reason"] for a in listed["alerts"]}
    assert reasons == {"cpu": "back_to_normal", "disk": "server_disconnected"}
    assert listed["counts"]["active"] == 0

    alert_id = listed["alerts"][0]["id"]
    assert (await api.get(f"/api/alerts/{alert_id}")).json()["status"] == "resolved"
    assert (await api.delete(f"/api/alerts/{alert_id}")).status_code == 204
    missing = await api.get(f"/api/alerts/{alert_id}")
    assert (missing.status_code, missing.json()["error"]) == (404, "alert_not_found")
    assert (await api.delete("/api/alerts")).status_code == 204
    assert (await api.get("/api/alerts")).json()["alerts"] == []

    reset = await api.delete("/api/alerts/rules")
    assert reset.json()["rules"][0]["warning"] == 90
    assert not settings.alerts_path.exists()
    bad = await api.put(
        "/api/alerts/rules", json={"rules": [{"metric": "cpu", "warning": 99, "critical": 1}]}
    )
    assert bad.status_code == 422


async def test_damaged_rules_file_does_not_break_the_plugin(alerts_api):
    """Файл испортили: плагин уже работает, правила по умолчанию, API отвечает."""
    api, _ = alerts_api
    assert (await api.get("/api/alerts/rules")).json()["rules"][0]["metric"] == "cpu"
    service = AlertService(RulesStore(settings.alerts_path))
    settings.alerts_path.parent.mkdir(parents=True, exist_ok=True)
    settings.alerts_path.write_text("not json", encoding="utf-8")
    await service.start()
    try:
        assert [r.metric for r in service.rules()] == [r.metric for r in DEFAULT_RULES]
    finally:
        await service.stop()


async def test_alerts_keep_the_monitor_polling(monkeypatch):
    monkeypatch.setattr(settings, "ALERTS_KEEP_POLLING", True)
    monkeypatch.setattr(settings, "MONITOR_DEMAND_TIMEOUT", 0.2)
    from app.plugins.monitor.service import metrics_monitor

    touched: list[bool] = []
    monkeypatch.setattr(metrics_monitor, "touch", lambda: touched.append(True))
    await alert_service.start()
    try:
        async with asyncio.timeout(5):
            while not touched:
                await asyncio.sleep(0.05)
    finally:
        await alert_service.stop()
