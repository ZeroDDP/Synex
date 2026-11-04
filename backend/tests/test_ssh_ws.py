"""WebSocket терминала /api/ssh/sessions/{id}/ws: привязка к сессии, повтор пропущенного,
команды из UI, закрытие сессии. Сервер настоящий (uvicorn), воркер фейковый."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.plugins.ssh import ws as ws_module
from app.plugins.ssh.manager import CLOSED_LOST_IN_WORKER, CLOSED_SHUTDOWN
from tests.ssh_helpers import make_api, recv_json, send_json, serve


@pytest.fixture
def connect_body(tmp_path: Path) -> dict:
    key = tmp_path / "id_ed25519"
    key.write_text("key")
    return {"host": "10.0.0.1", "username": "root", "key_path": str(key)}


async def _terminal(worker_state, missing_socket_path):
    client, harness = await make_api(worker_state.port, missing_socket_path)
    harness.api = client
    return harness


@pytest.fixture
async def term(shell_worker, missing_socket_path):
    """Плагин ssh на uvicorn поверх воркера, который реально выполняет команды."""
    harness = await _terminal(shell_worker, missing_socket_path)
    async with harness.api, serve(harness.app) as base:
        harness.base = base
        yield harness
    await harness.disconnect()


@pytest.fixture
async def fake_term(fake_worker, missing_socket_path):
    harness = await _terminal(fake_worker, missing_socket_path)
    async with harness.api, serve(harness.app) as base:
        harness.base = base
        yield harness
    await harness.disconnect()


async def open_session(harness, body: dict) -> str:
    response = await harness.api.post("/api/ssh/sessions", json=body)
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def ws_url(harness, session_id: str, since: int | None = None) -> str:
    url = f"{harness.base}/api/ssh/sessions/{session_id}/ws"
    return url if since is None else f"{url}?since={since}"


async def recv_until(ws, predicate, timeout: float = 5.0) -> list[dict]:
    """Сообщения до первого, для которого predicate(message) истинно (включительно)."""
    messages = []
    async with asyncio.timeout(timeout):
        while True:
            message = json.loads(await ws.recv())
            messages.append(message)
            if predicate(message):
                return messages


def is_result(message: dict) -> bool:
    return message["type"] == "output" and message["data"]["kind"] == "result"


# ------------------------------------------------------------- tests


async def test_hello_and_replay_of_earlier_output(term, connect_body):
    session_id = await open_session(term, connect_body)
    await term.api.post(f"/api/ssh/sessions/{session_id}/exec", json={"command": "echo before"})

    async with connect(ws_url(term, session_id)) as ws:
        hello = await recv_json(ws)
        assert hello["type"] == "hello"
        data = hello["data"]
        assert data["protocol"] == 1
        assert data["session"]["session_id"] == session_id
        assert data["session"]["attached"] == 1
        assert data["replay"] == {
            "since": 0,
            "count": 3,
            "gap": False,
            "first_seq": 1,
            "last_seq": 3,
        }

        replay = [await recv_json(ws) for _ in range(3)]
        assert [m["type"] for m in replay] == ["output"] * 3
        assert [(m["data"]["seq"], m["data"]["kind"]) for m in replay] == [
            (1, "opened"),
            (2, "command"),
            (3, "result"),
        ]
        assert replay[2]["data"]["stdout"] == "before"


async def test_exec_from_ws_reaches_every_viewer(term, connect_body):
    session_id = await open_session(term, connect_body)
    async with (
        connect(ws_url(term, session_id, since=1)) as first,
        connect(ws_url(term, session_id, since=1)) as second,
    ):
        for ws in (first, second):
            assert (await recv_json(ws))["data"]["replay"]["count"] == 0

        await send_json(first, "exec", {"command": "cd /tmp && echo hi", "id": "c1"})
        for ws in (first, second):
            command, result = await recv_until(ws, is_result)
            assert command["data"] | {"ts": 0} == {
                "seq": 2,
                "ts": 0,
                "kind": "command",
                "command": "cd /tmp && echo hi",
                "cwd": None,
                "source": "ws",
                "request_id": "c1",
            }
            assert result["data"]["seq"] == 3
            assert (result["data"]["stdout"], result["data"]["cwd"]) == ("hi", "/tmp")
            assert result["data"]["request_id"] == "c1"

        # Команда через REST тоже видна в терминале
        await term.api.post(f"/api/ssh/sessions/{session_id}/exec", json={"command": "pwd"})
        _, result = await recv_until(second, is_result)
        assert (result["data"]["stdout"], result["data"].get("request_id")) == ("/tmp", None)

    info = (await term.api.get(f"/api/ssh/sessions/{session_id}")).json()
    assert info["attached"] == 0  # закрытые вкладки отписались, сессия жива


async def test_reconnect_with_since_gets_only_missed_output(term, connect_body):
    session_id = await open_session(term, connect_body)
    async with connect(ws_url(term, session_id)) as ws:
        hello = await recv_json(ws)
        last_seen = hello["data"]["replay"]["last_seq"]

    # Вкладка закрыта, а команды продолжают идти
    for word in ("one", "two"):
        await term.api.post(
            f"/api/ssh/sessions/{session_id}/exec", json={"command": f"echo {word}"}
        )

    async with connect(ws_url(term, session_id, since=last_seen)) as ws:
        hello = await recv_json(ws)
        assert hello["data"]["replay"]["count"] == 4
        missed = [await recv_json(ws) for _ in range(4)]
        assert [m["data"]["seq"] for m in missed] == [2, 3, 4, 5]
        assert [m["data"].get("stdout") for m in missed if is_result(m)] == ["one", "two"]


async def test_command_keeps_running_after_ws_disconnects(term, connect_body):
    session_id = await open_session(term, connect_body)
    async with connect(ws_url(term, session_id)) as ws:
        await recv_json(ws)
        await send_json(ws, "exec", {"command": "sleep 0.3; echo done", "id": "bg"})
        await asyncio.sleep(0.1)

    await asyncio.sleep(0.6)
    output = (await term.api.get(f"/api/ssh/sessions/{session_id}/output?since=1")).json()
    result = output["entries"][-1]
    assert (result["kind"], result["stdout"], result["request_id"]) == ("result", "done", "bg")


async def test_close_from_ws_closes_session_for_every_viewer(term, connect_body):
    session_id = await open_session(term, connect_body)
    async with (
        connect(ws_url(term, session_id)) as first,
        connect(ws_url(term, session_id)) as second,
    ):
        await recv_until(first, lambda m: m["type"] == "output" and m["data"]["seq"] == 1)
        await recv_until(second, lambda m: m["type"] == "output" and m["data"]["seq"] == 1)
        await send_json(first, "close")
        for ws in (first, second):
            closed = await recv_json(ws)
            assert closed["type"] == "closed" and closed["data"] == {"reason": "closed"}
            with pytest.raises(ConnectionClosed) as excinfo:
                await recv_json(ws)
            assert excinfo.value.rcvd.code == 1000

    assert (await term.api.get(f"/api/ssh/sessions/{session_id}")).status_code == 404


async def test_unknown_session(term):
    async with connect(ws_url(term, "nope")) as ws:
        error = await recv_json(ws)
        assert error["type"] == "error"
        assert error["data"] == {
            "error": "ssh_session_not_found",
            "detail": "SSH session 'nope' not found",
        }
        with pytest.raises(ConnectionClosed) as excinfo:
            await recv_json(ws)
        assert excinfo.value.rcvd.code == 4404


async def test_ping_and_bad_messages(term, connect_body):
    session_id = await open_session(term, connect_body)
    async with connect(ws_url(term, session_id, since=1)) as ws:
        await recv_json(ws)
        await send_json(ws, "ping", {"n": 1})
        pong = await recv_json(ws)
        assert (pong["type"], pong["data"]) == ("pong", {"n": 1})

        await ws.send("not json")
        assert (await recv_json(ws))["data"]["error"] == "bad_message"
        await send_json(ws, "exec", {"timeout": 1})
        bad = await recv_json(ws)
        assert bad["data"]["error"] == "bad_message" and "command" in bad["data"]["detail"]
        await send_json(ws, "resize", {})
        assert (await recv_json(ws))["data"] == {
            "error": "unknown_type",
            "detail": "Unknown message type: resize",
        }


async def test_timeout_error_is_delivered_as_output(fake_term, connect_body):
    session_id = await open_session(fake_term, connect_body)
    async with connect(ws_url(fake_term, session_id, since=1)) as ws:
        await recv_json(ws)
        await send_json(
            ws, "exec", {"command": "sleep 1", "timeout": 0.1, "track_cwd": False, "id": "t1"}
        )
        messages = await recv_until(
            ws, lambda m: m["type"] == "output" and m["data"]["kind"] == "error"
        )
        error = messages[-1]["data"]
        assert (error["error"], error["request_id"]) == ("ssh_exec_timeout", "t1")


async def test_session_lost_in_worker(fake_term, fake_worker, connect_body):
    session_id = await open_session(fake_term, connect_body)
    async with connect(ws_url(fake_term, session_id, since=1)) as ws:
        await recv_json(ws)
        fake_worker.drop_sessions()
        await send_json(ws, "exec", {"command": "ls", "id": "x"})
        messages = await recv_until(ws, lambda m: m["type"] == "closed")
        assert messages[-1]["data"] == {"reason": CLOSED_LOST_IN_WORKER}
        with pytest.raises(ConnectionClosed):
            await recv_json(ws)


async def test_shutdown_closes_with_going_away(fake_term, connect_body):
    session_id = await open_session(fake_term, connect_body)
    async with connect(ws_url(fake_term, session_id, since=1)) as ws:
        await recv_json(ws)
        await fake_term.manager.close_all(reason=CLOSED_SHUTDOWN)
        assert (await recv_json(ws))["data"] == {"reason": CLOSED_SHUTDOWN}
        with pytest.raises(ConnectionClosed) as excinfo:
            await recv_json(ws)
        assert excinfo.value.rcvd.code == 1001


async def test_slow_client_is_closed_instead_of_losing_output(monkeypatch):
    """Очередь терминала не урезается: переполнилась, значит закрыть с 1013, чтобы UI
    переподключился с since и забрал пропущенное из буфера."""
    monkeypatch.setattr(ws_module, "MAX_PENDING_BYTES", 1_000)
    socket = ws_module.SessionSocket(websocket=None, manager=None, session=FakeSession())
    for _ in range(20):
        socket.push("output", {"stdout": "x" * 100})
    assert socket.close_code == ws_module.CLOSE_TRY_AGAIN_LATER
    assert socket.pending_bytes <= 1_000 + 200
    await socket.send_loop()  # сразу выходит: отправлять остальное не нужно


class FakeSession:
    id = "s"
