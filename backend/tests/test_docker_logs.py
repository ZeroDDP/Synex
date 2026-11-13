"""Живой лог контейнера по WebSocket (app/plugins/docker/logs.py). Docker здесь фейковый
(tests/fake_docker.py) в эмуляторе shell, поэтому тесты идут на любой ОС."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from app.core.config import settings
from app.plugins.docker.logs import active_streams, close_streams
from tests.fake_docker import FakeContainer, FakeDocker
from tests.plugin_rig import PluginRig, plugin_rig
from tests.ssh_helpers import recv_json, send_json, serve

LOGS = [
    ("2026-11-06T15:42:48.000000001Z", "stdout", "GET / 200"),
    ("2026-11-06T15:42:48.000000002Z", "stderr", "warning: slow"),
    ("2026-11-06T15:42:49.000000003Z", "stdout", "GET /api 500"),
]
LATER = "2026-11-06T15:42:5{}.000000000Z"


@dataclass
class Stream:
    rig: PluginRig
    docker: FakeDocker
    base: str  # ws://127.0.0.1:PORT

    def url(self, container: str = "web", query: str = "") -> str:
        path = f"/api/docker/sessions/{self.rig.session_id}/containers/{container}/logs/ws"
        return f"{self.base}{path}{query}"

    @asynccontextmanager
    async def open(
        self, container: str = "web", query: str = ""
    ) -> AsyncIterator[ClientConnection]:
        async with connect(self.url(container, query)) as ws:
            hello = await recv_json(ws)
            assert hello["type"] == "hello", hello
            yield ws

    def add(self, second: int, text: str, stream: str = "stdout") -> str:
        """Новая строка в логе контейнера web. Возвращает её время."""
        time = LATER.format(second)
        container = self.docker.find("web")
        assert container is not None
        container.logs.append((time, stream, text))
        return time

    def log_calls(self) -> list[list[str]]:
        return [call for call in self.docker.calls if call[0] == "logs"]


@pytest.fixture
async def stream(tmp_path: Path, missing_socket_path, monkeypatch) -> AsyncIterator[Stream]:
    monkeypatch.setattr(settings, "DOCKER_LOGS_INTERVAL", 0.05)
    docker = FakeDocker(
        containers=[FakeContainer(id="6de63364bd70" + "a" * 52, name="web", logs=list(LOGS))]
    )
    programs = {"docker": docker}
    async with plugin_rig(tmp_path, missing_socket_path, ["docker"], programs) as rig:
        async with serve(rig.app) as base:
            yield Stream(rig, docker, base)
    assert active_streams() == 0


async def next_of(ws: ClientConnection, message_type: str, timeout: float = 5.0) -> dict:
    async with asyncio.timeout(timeout):
        while True:
            message = json.loads(await ws.recv())
            if message["type"] == message_type:
                return message["data"]


async def closed_with(ws: ClientConnection, timeout: float = 5.0) -> int:
    """Код, с которым сервер закрыл подключение."""
    with pytest.raises(ConnectionClosed) as closed:
        async with asyncio.timeout(timeout):
            while True:
                await ws.recv()
    assert closed.value.rcvd is not None
    return closed.value.rcvd.code


def texts(data: dict) -> list[str]:
    return [line["text"] for line in data["lines"]]


async def test_stream_starts_with_the_tail_and_follows_new_lines(stream: Stream):
    async with connect(stream.url(query="?tail=2")) as ws:
        hello = await recv_json(ws)
        assert hello["type"] == "hello" and isinstance(hello["ts"], int)
        assert hello["data"] == {
            "protocol": 1,
            "session_id": stream.rig.session_id,
            "container": "web",
            "tail": 2,
            "interval_ms": 50,
        }

        first = await next_of(ws, "logs")
        assert first == {
            "lines": [
                {"time": LOGS[1][0], "stream": "stderr", "text": "warning: slow"},
                {"time": LOGS[2][0], "stream": "stdout", "text": "GET /api 500"},
            ],
            "next_since": LOGS[2][0],
            "truncated": False,
        }
        assert stream.log_calls()[0] == ["logs", "--timestamps", "--tail", "2", "web"]

        stream.add(1, "GET /new 200")
        newest = stream.add(2, "oops", "stderr")
        fresh = await next_of(ws, "logs")
        # Только новые строки: уже показанная (время = since) не повторяется
        assert [(line["text"], line["stream"]) for line in fresh["lines"]] == [
            ("GET /new 200", "stdout"),
            ("oops", "stderr"),
        ]
        assert fresh["next_since"] == newest
        assert stream.log_calls()[-1][:5] == ["logs", "--timestamps", "--tail", "10000", "--since"]

        stream.add(3, "third")
        assert texts(await next_of(ws, "logs")) == ["third"]
    await _settled()


async def _settled() -> bool:
    async with asyncio.timeout(5):
        while active_streams():
            await asyncio.sleep(0.01)
    return True


async def test_quiet_container_sends_nothing_after_the_first_batch(stream: Stream):
    async with stream.open() as ws:
        assert len((await next_of(ws, "logs"))["lines"]) == 3
        with pytest.raises(TimeoutError):
            await recv_json(ws, timeout=0.3)
        assert len(stream.log_calls()) >= 3  # опрос при этом идёт


async def test_empty_tail_still_answers_once(stream: Stream):
    async with stream.open(query="?tail=0") as ws:
        first = await next_of(ws, "logs")
        # Строк нет, но точка отсчёта есть: старый лог позже не придёт
        assert (first["lines"], first["next_since"]) == ([], LOGS[-1][0])
        stream.add(1, "after connect")
        assert texts(await next_of(ws, "logs")) == ["after connect"]


async def test_container_without_logs_yet(stream: Stream):
    container = stream.docker.find("web")
    assert container is not None
    container.logs.clear()
    async with stream.open(query="?tail=0") as ws:
        assert (await next_of(ws, "logs"))["lines"] == []
        stream.add(1, "first line ever")
        assert texts(await next_of(ws, "logs")) == ["first line ever"]


async def test_reconnect_with_since_does_not_repeat_lines(stream: Stream):
    async with stream.open() as ws:
        seen = await next_of(ws, "logs")
    stream.add(1, "while the UI was away")

    async with stream.open(query=f"?since={seen['next_since']}") as ws:
        assert texts(await next_of(ws, "logs")) == ["while the UI was away"]


async def test_ping_and_unknown_messages(stream: Stream):
    async with stream.open() as ws:
        await next_of(ws, "logs")
        await send_json(ws, "ping", {"n": 7})
        assert await next_of(ws, "pong") == {"n": 7}
        await ws.send("not json")
        error = await next_of(ws, "error")
        assert (error["error"], error["fatal"]) == ("bad_message", False)
        stream.add(1, "still streaming")
        assert texts(await next_of(ws, "logs")) == ["still streaming"]


async def test_missing_container_ends_the_stream(stream: Stream):
    async with stream.open(container="nope") as ws:
        error = await next_of(ws, "error")
        assert (error["error"], error["fatal"]) == ("docker_container_not_found", True)
        assert await closed_with(ws) == 4404


async def test_container_removed_during_the_stream(stream: Stream):
    async with stream.open() as ws:
        await next_of(ws, "logs")
        stream.docker.containers.clear()
        error = await next_of(ws, "error")
        assert (error["error"], error["fatal"]) == ("docker_container_not_found", True)
        assert await closed_with(ws) == 4404


async def test_docker_without_permission_ends_the_stream(stream: Stream):
    stream.docker.permission = False
    async with stream.open() as ws:
        error = await next_of(ws, "error")
        assert (error["error"], error["fatal"]) == ("docker_permission_denied", True)
        assert await closed_with(ws) == 4403


async def test_daemon_outage_is_reported_once_and_the_stream_recovers(stream: Stream):
    async with stream.open() as ws:
        await next_of(ws, "logs")
        stream.docker.daemon = False
        error = await next_of(ws, "error")
        assert (error["error"], error["fatal"]) == ("docker_daemon_unavailable", False)
        calls = len(stream.log_calls())
        with pytest.raises(TimeoutError):  # та же причина не повторяется на каждом опросе
            await recv_json(ws, timeout=0.3)
        assert len(stream.log_calls()) > calls

        stream.docker.daemon = True
        stream.add(1, "daemon is back")
        assert texts(await next_of(ws, "logs")) == ["daemon is back"]

        stream.docker.daemon = False  # новая помеха сообщается снова
        assert (await next_of(ws, "error"))["error"] == "docker_daemon_unavailable"


async def test_busy_session_pauses_the_stream(stream: Stream, monkeypatch):
    """Пока в сессии идёт команда пользователя, лог ждёт и сообщает об этом один раз."""
    async with stream.open() as ws:
        await next_of(ws, "logs")
        user = asyncio.create_task(
            stream.rig.manager.execute(stream.rig.session_id, "sleep 0.6", track_cwd=False)
        )
        stream.add(1, "written while busy")
        error = await next_of(ws, "error")
        assert (error["error"], error["fatal"]) == ("ssh_session_busy", False)
        assert texts(await next_of(ws, "logs")) == ["written while busy"]
        await user


async def test_closing_the_session_ends_the_stream(stream: Stream):
    async with stream.open() as ws:
        await next_of(ws, "logs")
        await stream.rig.manager.close(stream.rig.session_id)
        assert await next_of(ws, "closed") == {"reason": "session_closed"}
        assert await closed_with(ws) == 1000


async def test_unloading_the_plugin_closes_streams(stream: Stream):
    async with stream.open() as first, stream.open() as second:
        await next_of(first, "logs")
        await next_of(second, "logs")
        assert active_streams() == 2

        await close_streams()

        for ws in (first, second):
            assert await next_of(ws, "closed") == {"reason": "shutdown"}
            assert await closed_with(ws) == 1001
    assert active_streams() == 0


async def test_polling_stops_when_the_ui_disconnects(stream: Stream):
    async with stream.open() as ws:
        await next_of(ws, "logs")
    await _settled()
    calls = len(stream.log_calls())
    await asyncio.sleep(0.25)
    assert len(stream.log_calls()) == calls


async def test_bad_parameters_are_refused_before_the_stream_starts(stream: Stream):
    for query in ("?tail=100000", "?since=1;%20rm%20-rf%20/"):
        with pytest.raises(Exception) as refused:  # noqa: PT011
            async with connect(stream.url(query=query)) as ws:
                await ws.recv()
        assert "403" in str(refused.value) or "1008" in str(refused.value), refused.value
    assert stream.log_calls() == []
