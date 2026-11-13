"""Служебные сессии (open_companion) и действия воркера от имени плагина (request)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core.errors import WorkerCommandError
from app.plugins.ssh.errors import SSHNotConnectedError, SSHSessionBusyError
from tests.ssh_helpers import make_api


@pytest.fixture
def connect_body(tmp_path: Path) -> dict:
    key = tmp_path / "id_ed25519"
    key.write_text("key")
    return {"host": "10.0.0.1", "username": "root", "key_path": str(key)}


@pytest.fixture
async def ssh(fake_worker, missing_socket_path):
    client, harness = await make_api(fake_worker.port, missing_socket_path)
    async with client:
        harness.api = client
        yield harness
    await harness.disconnect()


async def open_session(harness, body: dict) -> str:
    response = await harness.api.post("/api/ssh/sessions", json=body)
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


async def test_companion_has_its_own_connection_and_is_hidden(ssh, fake_worker, connect_body):
    session_id = await open_session(ssh, connect_body)
    companion = await ssh.manager.open_companion(session_id, "test")
    assert companion is not None
    assert companion.id != session_id and companion.parent_id == session_id
    assert companion.channel.client is not ssh.manager.get(session_id).channel.client
    # Подключение тем же способом, что и основная сессия
    connects = [r for r in fake_worker.requests if r["action"] == "ssh_connect"]
    assert connects[-1]["id"] == companion.id
    assert connects[-1]["payload"]["connection"] == connects[0]["payload"]["connection"]
    # В списке сессий её нет
    listed = (await ssh.api.get("/api/ssh/sessions")).json()
    assert [s["session_id"] for s in listed] == [session_id]
    assert ssh.manager.companions(session_id) == [companion]

    # Долгое действие в служебной сессии не держит основную
    slow = asyncio.ensure_future(
        ssh.manager.companion_request(companion, "test_sleep", {"seconds": 0.5}, timeout=5)
    )
    await asyncio.sleep(0.05)
    result = await ssh.manager.execute(session_id, "echo main", track_cwd=False)
    assert result.stdout == "ran: echo main"
    assert not slow.done()
    await slow

    await ssh.manager.close_companion(companion)
    assert companion.closed and ssh.manager.companions(session_id) == []
    with pytest.raises(SSHNotConnectedError):
        await ssh.manager.companion_request(companion, "test_sleep", {}, timeout=5)


async def test_closing_the_companion_interrupts_its_action(ssh, connect_body):
    session_id = await open_session(ssh, connect_body)
    companion = await ssh.manager.open_companion(session_id, "test")
    assert companion is not None
    slow = asyncio.ensure_future(
        ssh.manager.companion_request(companion, "test_sleep", {"seconds": 5}, timeout=10)
    )
    await asyncio.sleep(0.05)
    await ssh.manager.close_companion(companion)
    with pytest.raises(SSHNotConnectedError):
        await slow


async def test_companions_close_with_their_session(ssh, connect_body):
    session_id = await open_session(ssh, connect_body)
    first = await ssh.manager.open_companion(session_id, "a")
    second = await ssh.manager.open_companion(session_id, "b")
    assert first is not None and second is not None
    await ssh.manager.close(session_id)
    assert first.closed and second.closed
    assert ssh.manager.companions(session_id) == []


async def test_no_companion_without_extra_connections(
    fake_worker, missing_socket_path, connect_body
):
    client, harness = await make_api(fake_worker.port, missing_socket_path, channels=False)
    async with client:
        harness.api = client
        session_id = await open_session(harness, connect_body)
        assert await harness.manager.open_companion(session_id, "test") is None
    await harness.disconnect()


async def test_request_runs_worker_actions_and_waits_for_a_busy_session(ssh, connect_body):
    session_id = await open_session(ssh, connect_body)
    reply = await ssh.manager.request(session_id, "test_sleep", {"echo": "hi"}, timeout=5)
    assert reply["stdout"] == "hi"
    with pytest.raises(WorkerCommandError, match="Unknown action"):
        await ssh.manager.request(session_id, "nope", timeout=5)

    user = asyncio.ensure_future(ssh.manager.execute(session_id, "sleep 0.5", track_cwd=False))
    await asyncio.sleep(0.05)
    with pytest.raises(SSHSessionBusyError):
        await ssh.manager.request(session_id, "test_sleep", timeout=5, wait=0)
    reply = await ssh.manager.request(session_id, "test_sleep", {"echo": "after"}, timeout=5)
    assert reply["stdout"] == "after" and user.done()
    # В терминал действия плагина не попадают
    entries = ssh.manager.get(session_id).buffer.since(0).entries
    assert [e.kind for e in entries] == ["opened", "command", "result"]
