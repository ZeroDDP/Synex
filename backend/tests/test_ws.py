"""WebSocket /ws: тестовые метрики, хаб и живое соединение через настоящий uvicorn."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import uvicorn
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from app import __version__
from app.core.app import create_app
from app.core.config import Settings, settings
from app.core.event_bus import event_bus
from app.worker import worker_manager
from app.ws import METRICS_EVENT, MetricsSnapshot, ws_hub
from app.ws.hub import WebSocketClient, WebSocketHub
from app.ws.metrics import FakeMetricsPublisher, FakeMetricsSource
from tests.conftest import FAKE_WORKER_SCRIPT, get_free_port

FAST_INTERVAL = 0.05

# ------------------------------------------------------------------ helpers


@pytest.fixture
def app_settings(monkeypatch, worker_settings):
    monkeypatch.setattr(
        worker_manager, "_worker_command", lambda: [sys.executable, str(FAKE_WORKER_SCRIPT)]
    )
    monkeypatch.setattr(settings, "WS_FAKE_METRICS", True)
    monkeypatch.setattr(settings, "WS_FAKE_METRICS_INTERVAL", FAST_INTERVAL)
    return settings


@asynccontextmanager
async def running_server() -> AsyncIterator[tuple[str, uvicorn.Server]]:
    """Приложение целиком (lifespan, воркер, плагины) на настоящем uvicorn."""
    port = get_free_port()
    config = uvicorn.Config(
        create_app(), host="127.0.0.1", port=port, log_config=None, log_level="warning"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(15):
            while not server.started:
                if task.done():
                    raise RuntimeError("uvicorn failed to start")
                await asyncio.sleep(0.02)
        yield f"ws://127.0.0.1:{port}/ws", server
    finally:
        server.should_exit = True
        async with asyncio.timeout(15):
            await task


@pytest.fixture
async def ws_url(app_settings) -> AsyncIterator[str]:
    async with running_server() as (url, _):
        yield url


async def recv_json(ws: ClientConnection, timeout: float = 5.0) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


async def recv_type(ws: ClientConnection, message_type: str, timeout: float = 5.0) -> dict:
    """Следующее сообщение нужного типа; метрики, идущие между делом, пропускаются."""
    async with asyncio.timeout(timeout):
        while True:
            message = json.loads(await ws.recv())
            if message["type"] == message_type:
                return message


async def wait_for_no_clients(timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while ws_hub.client_count:
            await asyncio.sleep(0.02)


# ------------------------------------------------------------- fake metrics


def test_fake_metrics_are_plausible():
    source = FakeMetricsSource(seed=1)
    for _ in range(500):
        snapshot = source.sample()
        assert snapshot.source == "fake" and snapshot.interval_ms == 500
        (server,) = snapshot.servers
        assert server.status == "ok" and server.server == "demo.synex.local:22"
        cpu, memory = server.cpu, server.memory
        assert 0 <= cpu.min <= cpu.percent <= cpu.max <= 100
        assert cpu.min <= cpu.avg <= cpu.max
        assert 0 < memory.used < memory.total
        assert memory.used + memory.available == memory.total
        assert memory.percent == pytest.approx(memory.used / memory.total * 100, abs=0.1)
        assert memory.min <= memory.percent <= memory.max


def test_fake_metrics_change_smoothly():
    source = FakeMetricsSource(seed=2)
    servers = [source.sample().servers[0] for _ in range(200)]
    memory = [s.memory.percent for s in servers]
    cpu = [s.cpu.percent for s in servers]
    # Значения живые...
    assert len(set(memory)) > 50 and len(set(cpu)) > 50
    # ...но без скачков: шаг ограничен (шум + притяжение к центру)
    assert max(abs(a - b) for a, b in zip(memory, memory[1:], strict=False)) < 10
    assert max(abs(a - b) for a, b in zip(cpu, cpu[1:], strict=False)) < 20


def test_fake_metrics_seed_is_reproducible():
    def values(source: FakeMetricsSource) -> list[dict]:
        return [
            source.sample().model_dump(exclude={"servers": {0: {"updated_at"}}}) for _ in range(5)
        ]

    assert values(FakeMetricsSource(seed=7)) == values(FakeMetricsSource(seed=7))


def test_default_interval_is_500_ms():
    assert Settings.model_fields["WS_FAKE_METRICS_INTERVAL"].default == 0.5
    assert Settings.model_fields["WS_FAKE_METRICS"].default is False  # настоящие метрики
    assert Settings.model_fields["MONITOR_PUBLISH_INTERVAL"].default == 0.5
    with pytest.raises(ValueError):
        FakeMetricsPublisher(0)


async def test_publisher_keeps_interval_and_stops():
    times: list[float] = []

    async def on_metrics(data: MetricsSnapshot) -> None:
        assert isinstance(data, MetricsSnapshot)
        times.append(time.monotonic())

    event_bus.subscribe(METRICS_EVENT, on_metrics)
    publisher = FakeMetricsPublisher(0.1)
    try:
        publisher.start()
        async with asyncio.timeout(5):
            while len(times) < 6:
                await asyncio.sleep(0.01)
        await publisher.stop()
        assert not publisher.running
        # 5 периодов по 100 мс; расписание считается от старта, поэтому без дрейфа
        assert 0.4 <= times[5] - times[0] <= 0.7

        published = len(times)
        await asyncio.sleep(0.3)
        assert len(times) == published
    finally:
        await publisher.stop()
        event_bus.unsubscribe(METRICS_EVENT, on_metrics)


# ---------------------------------------------------------------------- hub


def test_slow_client_keeps_only_freshest_messages():
    client = WebSocketClient(websocket=None, queue_size=3)  # type: ignore[arg-type]
    for i in range(5):
        client.push(str(i))
    assert [client.queue.get_nowait() for _ in range(3)] == ["2", "3", "4"]
    assert client.dropped == 2


async def test_hub_forwards_bus_events_until_stopped():
    hub = WebSocketHub()
    client = hub.register(websocket=None)  # type: ignore[arg-type]
    hub.forward("demo")
    hub.forward("demo")  # повторная подписка ничего не дублирует
    assert hub.streams == ["demo"]
    try:
        await event_bus.publish("demo", FakeMetricsSource(seed=1).sample())
        message = json.loads(client.queue.get_nowait())
        assert client.queue.empty()
        assert message["type"] == "demo"
        assert abs(message["ts"] - time.time() * 1000) < 5000
        MetricsSnapshot.model_validate(message["data"])
    finally:
        hub.stop_forwarding()

    assert hub.streams == []
    await event_bus.publish("demo", {"x": 1})
    assert client.queue.empty()


# --------------------------------------------------------- live connection


async def test_ws_sends_hello_then_metrics(ws_url):
    async with connect(ws_url) as ws:
        hello = await recv_json(ws)
        assert hello["type"] == "hello"
        assert hello["data"] == {
            "app": settings.APP_NAME,
            "version": __version__,
            "protocol": 1,
            "streams": [METRICS_EVENT],
        }

        messages = [await recv_json(ws) for _ in range(5)]
        assert [m["type"] for m in messages] == ["metrics"] * 5
        for message in messages:
            assert MetricsSnapshot.model_validate(message["data"]).source == "fake"
        stamps = [m["ts"] for m in messages]
        assert stamps == sorted(stamps)


async def test_ws_metrics_every_500_ms(app_settings, monkeypatch):
    monkeypatch.setattr(settings, "WS_FAKE_METRICS_INTERVAL", 0.5)
    async with running_server() as (url, _), connect(url) as ws:
        assert (await recv_json(ws))["type"] == "hello"
        stamps = [(await recv_type(ws, "metrics"))["ts"] for _ in range(3)]
    for earlier, later in zip(stamps, stamps[1:], strict=False):
        assert 400 <= later - earlier <= 700


async def test_ws_ping_pong_and_bad_messages(ws_url):
    async with connect(ws_url) as ws:
        await ws.send(json.dumps({"type": "ping", "data": {"id": 7}}))
        assert (await recv_type(ws, "pong"))["data"] == {"id": 7}

        bad = ["not json", "[1, 2]", json.dumps({"data": 1}), b"\x00\x01"]
        for payload in bad:
            await ws.send(payload)
            error = (await recv_type(ws, "error"))["data"]
            assert error["error"] == "bad_message", payload

        await ws.send(json.dumps({"type": "subscribe"}))
        error = (await recv_type(ws, "error"))["data"]
        assert error == {"error": "unknown_type", "detail": "Unknown message type: subscribe"}

        # После ошибок соединение живо и метрики идут дальше
        await recv_type(ws, "metrics")


async def test_ws_serves_several_clients_and_forgets_closed_ones(ws_url):
    async with connect(ws_url) as first, connect(ws_url) as second:
        for ws in (first, second):
            await recv_type(ws, "metrics")
        assert ws_hub.client_count == 2
    await wait_for_no_clients()


async def test_server_shutdown_closes_ws(app_settings):
    async with running_server() as (url, server):
        ws = await connect(url)
        await recv_type(ws, "metrics")
        server.should_exit = True
        with pytest.raises(ConnectionClosed) as closed:
            async with asyncio.timeout(5):
                while True:
                    await ws.recv()
        # uvicorn закрывает соединения кодом 1012 (service restart): UI переподключается
        assert closed.value.rcvd is not None and closed.value.rcvd.code == 1012
    assert ws_hub.client_count == 0
    assert ws_hub.streams == []
