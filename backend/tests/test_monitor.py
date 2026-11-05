"""Мониторинг серверов (плагин monitor): разбор ответа воркера, агрегация, опрос через
свободную сессию, статусы, публикация в /ws и GET /api/monitor/metrics.

Только фейковый воркер без shell: тесты одинаково работают на Windows и Unix.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from websockets.asyncio.client import connect

from app.api.router import build_api_router
from app.core.app import create_app
from app.core.config import settings
from app.core.errors import register_error_handlers
from app.core.event_bus import event_bus
from app.plugins.manager import PluginManager
from app.plugins.monitor.aggregator import Sample, ServerState
from app.plugins.monitor.routes import get_monitor
from app.plugins.monitor.service import MetricsMonitor
from app.plugins.monitor.values import MonitorValuesError, Values, parse_values
from app.plugins.ssh.manager import ConnectParams
from app.plugins.ssh.routes import get_ssh_manager
from app.worker import worker_manager
from app.ws.metrics import METRICS_EVENT, MetricsSnapshot
from tests.conftest import FAKE_WORKER_SCRIPT, start_fake_worker
from tests.fake_worker import BOOT_CPU, MEM_TOTAL_KB, FakeWorkerState
from tests.ssh_helpers import make_api, recv_json, serve

GIB = 1024**3


@pytest.fixture(autouse=True)
def fast_monitor(monkeypatch):
    monkeypatch.setattr(settings, "MONITOR_POLL_INTERVAL", 0.05)
    monkeypatch.setattr(settings, "MONITOR_PUBLISH_INTERVAL", 0.05)
    monkeypatch.setattr(settings, "MONITOR_STALE_AFTER", 0.5)
    monkeypatch.setattr(settings, "MONITOR_WINDOW", 30.0)
    monkeypatch.setattr(settings, "MONITOR_POLL_TIMEOUT", 2.0)
    return settings


@pytest.fixture
def key_file(tmp_path: Path) -> str:
    key = tmp_path / "id_ed25519"
    key.write_text("key")
    return str(key)


class Rig:
    """Менеджер сессий на фейковом воркере и монитор поверх него."""

    def __init__(self, harness, state: FakeWorkerState, key_file: str):
        self.harness = harness
        self.manager = harness.manager
        self.state = state
        self.key_file = key_file
        self.viewers = True
        self.monitor = MetricsMonitor(self.manager, demand=lambda: self.viewers)

    async def open(self, host: str, port: int = 22) -> str:
        params = ConnectParams(host=host, port=port, username="root", key_path=self.key_file)
        return (await self.manager.open(params, keys=None)).id  # type: ignore[arg-type]

    async def poll(self) -> None:
        await asyncio.gather(*await self.monitor.poll())

    def server(self, key: str):
        snapshot = self.monitor.snapshot()
        return next(s for s in snapshot.servers if s.server == key)

    def monitor_requests(self) -> list[str]:
        return [r["id"] for r in self.state.requests if r["action"] == "monitor_get_values"]


async def make_rig(state: FakeWorkerState, missing_socket_path: str, key_file: str):
    client, harness = await make_api(state.port, missing_socket_path)
    await client.aclose()
    return Rig(harness, state, key_file)


@pytest.fixture
async def rig(fake_worker, missing_socket_path, key_file):
    rig = await make_rig(fake_worker, missing_socket_path, key_file)
    yield rig
    await rig.monitor.stop()
    await rig.harness.disconnect()


# ------------------------------------------------------------ parsing


def test_parse_values():
    response = {
        "stdout": '{"ok":true,"cpu_usage_percent":2.63,"mem_total_kb":2014404,'
        '"mem_available_kb":793976,"mem_used_kb":1220428,"mem_usage_percent":60.59,'
        '"error":null}',
        "stderr": "",
        "exit_code": 0,
    }
    assert parse_values(response) == Values(
        cpu_percent=2.63,
        mem_total=2014404 * 1024,
        mem_used=1220428 * 1024,
        mem_available=793976 * 1024,
        mem_percent=60.59,
    )


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            {
                "stdout": "Пустой ответ от сервера (команда не выполнилась)",
                "stderr": "bash: head: command not found",
            },
            "bash: head: command not found",
        ),
        ({"stdout": '{"ok": false, "error": "no /proc"}'}, "no /proc"),
        ({"stdout": '{"ok": true, "cpu_usage_percent": 1}'}, "Incomplete metrics"),
        ({"stdout": "[1, 2]"}, "unexpected JSON"),
        ({"stdout": ""}, "empty output"),
    ],
)
def test_parse_values_errors(response, message):
    with pytest.raises(MonitorValuesError, match=message):
        parse_values(response)


# ---------------------------------------------------------- aggregator


def values(cpu: float = 5.0, mem_percent: float = 50.0) -> Values:
    return Values(
        cpu_percent=cpu,
        mem_total=8 * GIB,
        mem_used=int(8 * GIB * mem_percent / 100),
        mem_available=8 * GIB - int(8 * GIB * mem_percent / 100),
        mem_percent=mem_percent,
    )


def test_server_state_statuses_and_window():
    state = ServerState("Example.com", 22, window=10.0)
    assert state.key == "example.com:22"
    empty = state.metrics(["s1"], stale_after=3.0, now=100.0, now_ms=0)
    assert (empty.status, empty.cpu, empty.memory) == ("waiting", None, None)

    # Первый замер сессии: CPU от загрузки сервера отброшен, память уже есть
    state.record(Sample(at=100.0, wall_ms=1_000, cpu=None, values=values(cpu=2.63)))
    warming = state.metrics(["s1"], stale_after=3.0, now=100.1, now_ms=1_100)
    assert warming.status == "warming_up"
    assert warming.cpu is not None and warming.cpu.percent is None
    assert warming.memory is not None and warming.memory.total == 8 * GIB
    assert (warming.updated_at, warming.age_ms) == (1_000, 100)

    for i, (cpu, mem) in enumerate([(10.0, 40.0), (30.0, 60.0), (20.0, 50.0)], start=1):
        state.record(Sample(at=100.0 + i, wall_ms=1_000 + i, cpu=cpu, values=values(cpu, mem)))
    ok = state.metrics(["s1", "s2"], stale_after=3.0, now=103.5, now_ms=0)
    assert ok.status == "ok" and ok.session_ids == ["s1", "s2"]
    assert ok.cpu.model_dump() == {"percent": 20.0, "avg": 20.0, "min": 10.0, "max": 30.0}
    assert (ok.memory.percent, ok.memory.min, ok.memory.max) == (50.0, 40.0, 60.0)

    # Старые замеры уходят из окна (10 с), последний остаётся всегда
    later = state.metrics(["s1"], stale_after=3.0, now=112.5, now_ms=0)
    assert later.status == "stale" and later.age_ms == 9_500
    assert later.cpu.model_dump() == {"percent": 20.0, "avg": 20.0, "min": 20.0, "max": 20.0}

    state.record_busy()
    assert state.metrics(["s1"], stale_after=3.0, now=112.5, now_ms=0).busy is True
    state.record_error("No metrics")
    failed = state.metrics(["s1"], stale_after=3.0, now=112.5, now_ms=0)
    assert (failed.status, failed.error) == ("error", "No metrics")
    assert failed.memory is not None  # последние известные значения остаются

    state.record(Sample(at=113.0, wall_ms=0, cpu=5.0, values=values()))
    back = state.metrics(["s1"], stale_after=3.0, now=113.0, now_ms=0)
    assert (back.status, back.error, back.busy) == ("ok", None, False)


# ------------------------------------------------------------- polling


async def test_each_server_is_polled_once_through_one_session(rig):
    a1 = await rig.open("server-a")
    a2 = await rig.open("server-a")
    b1 = await rig.open("server-b", port=2222)

    await rig.poll()
    snapshot = rig.monitor.snapshot()
    assert [s.server for s in snapshot.servers] == ["server-a:22", "server-b:2222"]
    server_a = rig.server("server-a:22")
    assert server_a.session_ids == [a1, a2]
    assert server_a.status == "warming_up"  # первый замер: CPU от загрузки сервера
    assert server_a.cpu.percent is None
    assert server_a.memory.total == MEM_TOTAL_KB * 1024

    await rig.poll()
    await rig.poll()
    server_a = rig.server("server-a:22")
    assert server_a.status == "ok"
    assert server_a.cpu.model_dump() == {"percent": 11.0, "avg": 10.5, "min": 10.0, "max": 11.0}
    assert BOOT_CPU not in (server_a.cpu.min, server_a.cpu.max)

    # 3 опроса на сервер, все через одну и ту же сессию
    requests = rig.monitor_requests()
    assert len(requests) == 6
    assert {sid for sid in requests if sid in (a1, a2)} == {a1}
    assert requests.count(b1) == 3
    assert snapshot.source == "ssh" and snapshot.interval_ms == 50


async def test_busy_session_is_skipped(rig):
    first = await rig.open("server-a")
    second = await rig.open("server-a")
    await rig.poll()
    assert rig.monitor_requests() == [first]

    # Первая сессия занята долгой командой: опрос идёт через вторую и не ждёт
    command = asyncio.create_task(rig.manager.execute(first, "sleep 0.5", track_cwd=False))
    await asyncio.sleep(0.05)
    started = time.monotonic()
    await rig.poll()
    assert time.monotonic() - started < 0.3
    assert rig.monitor_requests() == [first, second]
    await command


async def test_server_with_all_sessions_busy_becomes_stale(rig):
    session = await rig.open("server-a")
    await rig.poll()
    await rig.poll()
    assert rig.server("server-a:22").status == "ok"

    command = asyncio.create_task(rig.manager.execute(session, "sleep 0.9", track_cwd=False))
    await asyncio.sleep(0.05)
    await rig.poll()
    busy = rig.server("server-a:22")
    assert busy.busy is True and busy.status == "ok"  # данные ещё свежие
    await asyncio.sleep(0.6)
    stale = rig.server("server-a:22")
    assert (stale.status, stale.busy) == ("stale", True)
    assert stale.memory is not None and stale.cpu.percent == 10.0  # последние известные
    assert len(rig.monitor_requests()) == 2  # пока сессия занята, опросов не было

    await command
    await rig.poll()
    assert (rig.server("server-a:22").status, rig.server("server-a:22").busy) == ("ok", False)


async def test_server_without_metrics_command(rig):
    await rig.open("no-proc")
    await rig.poll()
    server = rig.server("no-proc:22")
    assert server.status == "error"
    assert "bash: head: command not found" in server.error
    assert server.memory is None


async def test_worker_reports_ok_false(missing_socket_path, key_file):
    state = FakeWorkerState(monitor_error="Cannot read /proc/stat")
    server = await start_fake_worker(state)
    rig = await make_rig(state, missing_socket_path, key_file)
    try:
        await rig.open("server-a")
        await rig.poll()
        result = rig.server("server-a:22")
        assert (result.status, result.error) == ("error", "Cannot read /proc/stat")
    finally:
        await rig.harness.disconnect()
        server.close()
        await server.wait_closed()


async def test_closed_session_removes_server(rig):
    session = await rig.open("server-a")
    await rig.poll()
    await rig.manager.close(session)
    assert rig.monitor.snapshot().servers == []


async def test_no_polling_without_viewers(rig):
    rig.viewers = False
    await rig.open("server-a")
    await rig.poll()
    assert rig.monitor_requests() == []
    assert rig.server("server-a:22").status == "waiting"


async def test_publishes_snapshot_every_interval(rig):
    await rig.open("server-a")
    snapshots: list[tuple[float, MetricsSnapshot]] = []

    async def collect(data: MetricsSnapshot) -> None:
        snapshots.append((time.monotonic(), data))

    event_bus.subscribe(METRICS_EVENT, collect)
    try:
        rig.monitor.start()
        async with asyncio.timeout(5):
            while len(snapshots) < 8:
                await asyncio.sleep(0.01)
        await rig.monitor.stop()
    finally:
        event_bus.unsubscribe(METRICS_EVENT, collect)

    times = [t for t, _ in snapshots]
    # 5 периодов по 50 мс. Тики без дрейфа, но любой из них может начаться на пару мс
    # позже (event loop занят), поэтому точное 0.25 как нижняя граница даёт ложные падения
    assert 0.2 <= times[7] - times[2] <= 0.5
    last = snapshots[-1][1]
    assert isinstance(last, MetricsSnapshot) and last.source == "ssh"
    assert last.servers[0].status == "ok"


async def test_new_session_is_polled_right_away(rig, monkeypatch):
    monkeypatch.setattr(settings, "MONITOR_POLL_INTERVAL", 30.0)
    rig.monitor.start(publish=False)
    await asyncio.sleep(0.05)  # первый тик прошёл, следующий через 30 с
    await rig.open("server-a")
    async with asyncio.timeout(2):
        while rig.server("server-a:22").status == "waiting":
            await asyncio.sleep(0.01)


# ---------------------------------------------------------------- REST


async def test_rest_metrics_turn_polling_on(rig):
    monitor = MetricsMonitor(rig.manager)  # без /ws-клиентов: опрос только по запросу
    app = FastAPI()
    register_error_handlers(app)
    api_router = build_api_router()
    PluginManager().discover(api_router, enabled=["ssh", "monitor"])
    app.include_router(api_router)
    app.dependency_overrides[get_ssh_manager] = lambda: rig.manager
    app.dependency_overrides[get_monitor] = lambda: monitor
    await rig.open("server-a")
    monitor.start(publish=False)
    try:
        await asyncio.sleep(0.2)
        assert rig.monitor_requests() == []  # метрики никому не нужны

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
            first = (await api.get("/api/monitor/metrics")).json()
            assert first["servers"][0]["status"] == "waiting"
            async with asyncio.timeout(3):
                while (await api.get("/api/monitor/metrics")).json()["servers"][0][
                    "status"
                ] != "ok":
                    await asyncio.sleep(0.05)
        MetricsSnapshot.model_validate(first)
    finally:
        await monitor.stop()


# --------------------------------------------------------- whole app


@pytest.fixture
def app_settings(monkeypatch, worker_settings, fast_monitor):
    monkeypatch.setattr(
        worker_manager, "_worker_command", lambda: [sys.executable, str(FAKE_WORKER_SCRIPT)]
    )
    monkeypatch.setattr(settings, "WS_FAKE_METRICS", False)
    monkeypatch.setattr(settings, "MONITOR_PUBLISH_INTERVAL", 0.1)
    monkeypatch.setattr(settings, "PLUGINS_ENABLED", "ssh,keys,monitor")
    return settings


async def recv_metrics(ws) -> dict:
    """Следующее сообщение metrics: в /ws идут и другие события (ssh.session и т.п.)."""
    while True:
        message = await recv_json(ws)
        if message["type"] == METRICS_EVENT:
            return message


async def test_ws_streams_server_metrics(app_settings, key_file):
    """Всё приложение: SSH-сессия через REST, метрики её сервера приходят в /ws."""
    async with serve(create_app()) as base:
        async with (
            connect(f"{base}/ws") as ws,
            httpx.AsyncClient(base_url=base.replace("ws://", "http://")) as api,
        ):
            hello = await recv_json(ws)
            assert METRICS_EVENT in hello["data"]["streams"]
            empty = await recv_metrics(ws)
            assert empty["data"]["servers"] == []

            body = {"host": "10.0.0.1", "username": "root", "key_path": key_file}
            created = await api.post("/api/ssh/sessions", json=body)
            assert created.status_code == 201, created.text
            session_id = created.json()["session_id"]

            stamps = []
            async with asyncio.timeout(5):
                while True:
                    message = await recv_metrics(ws)  # ssh.session opened пропускается
                    stamps.append(message["ts"])
                    data = MetricsSnapshot.model_validate(message["data"])
                    if data.servers and data.servers[0].status == "ok":
                        break
            server = data.servers[0]
            assert (server.server, server.session_ids) == ("10.0.0.1:22", [session_id])
            assert server.cpu.percent is not None and server.memory.total > 0
            gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
            assert all(50 <= gap <= 300 for gap in gaps), gaps
