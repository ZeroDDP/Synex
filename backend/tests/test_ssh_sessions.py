"""Менеджер SSH-сессий: /api/ssh/sessions, отдельные подключения к воркеру, буфер
вывода, закрытие и потеря сессий, события ssh.session."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from app.core.config import settings
from app.core.event_bus import event_bus
from app.plugins.ssh.manager import (
    CLOSED_BY_USER,
    CLOSED_WORKER_LOST,
    SSH_SESSION_EVENT,
)
from app.worker.manager import STATUS_WORKER_EXITED, WORKER_STATUS_EVENT
from tests.ssh_helpers import make_api


@pytest.fixture
def key_file(tmp_path: Path) -> str:
    key = tmp_path / "id_ed25519"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n")
    return str(key)


@pytest.fixture
def connect_body(key_file: str) -> dict:
    return {"host": "10.0.0.1", "username": "root", "key_path": key_file}


@pytest.fixture
async def ssh(fake_worker, missing_socket_path):
    client, harness = await make_api(fake_worker.port, missing_socket_path)
    async with client:
        harness.api = client
        yield harness
    await harness.disconnect()


@pytest.fixture
async def shell_ssh(shell_worker, missing_socket_path):
    client, harness = await make_api(shell_worker.port, missing_socket_path)
    async with client:
        harness.api = client
        yield harness
    await harness.disconnect()


@pytest.fixture
def events():
    received: list[dict] = []

    async def collect(event: dict) -> None:
        received.append(event)

    event_bus.subscribe(SSH_SESSION_EVENT, collect)
    yield received
    event_bus.unsubscribe(SSH_SESSION_EVENT, collect)


async def open_session(ssh, body: dict) -> dict:
    response = await ssh.api.post("/api/ssh/sessions", json=body)
    assert response.status_code == 201, response.text
    return response.json()


# ------------------------------------------------------------------ CRUD


async def test_sessions_crud(ssh, fake_worker, connect_body, events):
    info = await open_session(ssh, connect_body)
    session_id = info["session_id"]
    assert info["channel"] == "dedicated"
    assert (info["busy"], info["waiting"], info["attached"], info["last_seq"]) == (
        False,
        0,
        0,
        1,
    )
    assert info["host"] == "10.0.0.1" and info["auth"] == "key_path"

    listed = (await ssh.api.get("/api/ssh/sessions")).json()
    assert [s["session_id"] for s in listed] == [session_id]
    assert (await ssh.api.get(f"/api/ssh/sessions/{session_id}")).json() == listed[0]

    closed = await ssh.api.delete(f"/api/ssh/sessions/{session_id}")
    assert closed.status_code == 200
    assert closed.json() == {
        "status": "disconnected",
        "session_id": session_id,
        "message": f"Disconnected id: {session_id}",
    }
    assert (await ssh.api.get(f"/api/ssh/sessions/{session_id}")).status_code == 404
    assert (await ssh.api.get("/api/ssh/sessions")).json() == []
    assert ssh.provider.open_channels == 0  # отдельное подключение закрыто
    assert fake_worker.sessions == set()

    assert [(e["action"], e["session_id"]) for e in events] == [
        ("opened", session_id),
        ("closed", session_id),
    ]
    assert events[1]["reason"] == CLOSED_BY_USER
    assert events[0]["host"] == "10.0.0.1"


async def test_unknown_session_paths(ssh):
    for method, path in [
        ("GET", "/api/ssh/sessions/nope"),
        ("DELETE", "/api/ssh/sessions/nope"),
        ("GET", "/api/ssh/sessions/nope/output"),
    ]:
        response = await ssh.api.request(method, path)
        assert response.status_code == 404, (method, path)
        assert response.json()["error"] == "ssh_session_not_found"
    response = await ssh.api.post("/api/ssh/sessions/nope/exec", json={"command": "ls"})
    assert response.status_code == 404
    assert (await ssh.api.get("/api/ssh/sessions/bad%20id")).status_code == 422


async def test_session_limit(ssh, connect_body, monkeypatch):
    monkeypatch.setattr(settings, "SSH_MAX_SESSIONS", 2)
    await open_session(ssh, connect_body)
    await open_session(ssh, connect_body)
    response = await ssh.api.post("/api/ssh/sessions", json=connect_body)
    assert response.status_code == 429
    assert response.json()["error"] == "ssh_too_many_sessions"
    assert ssh.provider.open_channels == 2  # лишнее подключение не открывалось


async def test_failed_connect_closes_its_channel(ssh, connect_body):
    response = await ssh.api.post("/api/ssh/sessions", json={**connect_body, "host": "unreachable"})
    assert response.status_code == 502
    assert ssh.provider.open_channels == 0
    assert len(ssh.provider.opened) == 1


# ---------------------------------------------------- worker connections


async def test_each_session_has_its_own_worker_connection(ssh, fake_worker, connect_body):
    first = (await open_session(ssh, connect_body))["session_id"]
    second = (await open_session(ssh, connect_body))["session_id"]
    assert ssh.provider.open_channels == 2
    # В каждом подключении воркера ровно одна своя сессия
    owners = [set(p.sessions) for p in fake_worker.protocols if p.sessions]
    assert sorted(owners, key=sorted) == sorted([{first}, {second}], key=sorted)


async def test_sessions_run_commands_in_parallel(shell_ssh, connect_body):
    """Воркер выполняет запросы одного подключения по очереди: параллельность даёт
    только отдельное подключение на сессию."""
    first = (await open_session(shell_ssh, connect_body))["session_id"]
    second = (await open_session(shell_ssh, connect_body))["session_id"]

    async def run(session_id: str, command: str) -> dict:
        response = await shell_ssh.api.post(
            f"/api/ssh/sessions/{session_id}/exec", json={"command": command}
        )
        assert response.status_code == 200, response.text
        return response.json()

    started = time.monotonic()
    a, b = await asyncio.gather(run(first, "sleep 0.6; echo a"), run(second, "sleep 0.6; echo b"))
    assert (a["stdout"], b["stdout"]) == ("a", "b")
    assert time.monotonic() - started < 1.1


async def test_falls_back_to_shared_connection(shell_worker, missing_socket_path, connect_body):
    client, harness = await make_api(shell_worker.port, missing_socket_path, channels=False)
    try:
        async with client:
            harness.api = client
            first = await open_session(harness, connect_body)
            second = await open_session(harness, connect_body)
            assert first["channel"] == second["channel"] == "shared"

            started = time.monotonic()
            await asyncio.gather(
                client.post(
                    f"/api/ssh/sessions/{first['session_id']}/exec",
                    json={"command": "sleep 0.4"},
                ),
                client.post(
                    f"/api/ssh/sessions/{second['session_id']}/exec",
                    json={"command": "sleep 0.4"},
                ),
            )
            assert time.monotonic() - started >= 0.8  # по очереди

            await client.delete(f"/api/ssh/sessions/{first['session_id']}")
            assert harness.provider.main.connected  # общее подключение не закрывается
            reply = await client.post(
                f"/api/ssh/sessions/{second['session_id']}/exec", json={"command": "echo ok"}
            )
            assert reply.json()["stdout"] == "ok"
    finally:
        await harness.disconnect()


# --------------------------------------------------------- output buffer


async def test_output_is_buffered(shell_ssh, connect_body):
    session_id = (await open_session(shell_ssh, connect_body))["session_id"]
    exec_url = f"/api/ssh/sessions/{session_id}/exec"
    first = (await shell_ssh.api.post(exec_url, json={"command": "cd /tmp && echo one"})).json()
    await shell_ssh.api.post(exec_url, json={"command": "echo two >&2; exit 3"})
    assert first["seq"] == 3

    output = (await shell_ssh.api.get(f"/api/ssh/sessions/{session_id}/output")).json()
    entries = output["entries"]
    assert (output["first_seq"], output["last_seq"], output["gap"]) == (1, 5, False)
    assert [(e["seq"], e["kind"]) for e in entries] == [
        (1, "opened"),
        (2, "command"),
        (3, "result"),
        (4, "command"),
        (5, "result"),
    ]
    assert entries[0]["host"] == "10.0.0.1"
    assert entries[1] == {
        "seq": 2,
        "ts": entries[1]["ts"],
        "kind": "command",
        "command": "cd /tmp && echo one",
        "cwd": None,
        "source": "api",
    }
    assert (entries[2]["stdout"], entries[2]["exit_code"], entries[2]["cwd"]) == ("one", 0, "/tmp")
    assert entries[3]["cwd"] == "/tmp"
    assert (entries[4]["stderr"], entries[4]["exit_code"]) == ("two", 3)

    later = (await shell_ssh.api.get(f"/api/ssh/sessions/{session_id}/output?since=3")).json()
    assert [e["seq"] for e in later["entries"]] == [4, 5]
    limited = await shell_ssh.api.get(f"/api/ssh/sessions/{session_id}/output?since=0&limit=2")
    assert [e["seq"] for e in limited.json()["entries"]] == [1, 2]

    info = (await shell_ssh.api.get(f"/api/ssh/sessions/{session_id}")).json()
    assert (info["last_seq"], info["cwd"]) == (5, "/tmp")


async def test_worker_errors_are_buffered(ssh, connect_body):
    session_id = (await open_session(ssh, connect_body))["session_id"]
    response = await ssh.api.post(
        f"/api/ssh/sessions/{session_id}/exec",
        json={"command": "sleep 1", "timeout": 0.1, "track_cwd": False},
    )
    assert response.status_code == 504
    output = (await ssh.api.get(f"/api/ssh/sessions/{session_id}/output?since=1")).json()
    kinds = [(e["kind"], e.get("error")) for e in output["entries"]]
    assert kinds == [("command", None), ("error", "ssh_exec_timeout")]


async def test_busy_and_waiting(ssh, connect_body):
    session_id = (await open_session(ssh, connect_body))["session_id"]
    exec_url = f"/api/ssh/sessions/{session_id}/exec"
    body = {"command": "sleep 0.4", "track_cwd": False}
    running = asyncio.create_task(ssh.api.post(exec_url, json=body))
    queued = asyncio.create_task(ssh.api.post(exec_url, json=body))
    await asyncio.sleep(0.15)

    info = (await ssh.api.get(f"/api/ssh/sessions/{session_id}")).json()
    assert (info["busy"], info["waiting"]) == (True, 1)
    await asyncio.gather(running, queued)
    info = (await ssh.api.get(f"/api/ssh/sessions/{session_id}")).json()
    assert (info["busy"], info["waiting"]) == (False, 0)


# ------------------------------------------------------- close and loss


async def test_close_while_command_is_running(ssh, fake_worker, connect_body):
    session_id = (await open_session(ssh, connect_body))["session_id"]
    running = asyncio.create_task(
        ssh.api.post(
            f"/api/ssh/sessions/{session_id}/exec",
            json={"command": "sleep 5", "track_cwd": False},
        )
    )
    await asyncio.sleep(0.1)

    started = time.monotonic()
    closed = await ssh.api.delete(f"/api/ssh/sessions/{session_id}")
    assert closed.status_code == 200
    assert "while a command was running" in closed.json()["message"]
    reply = await running
    assert time.monotonic() - started < 1.0  # не ждали окончания sleep 5

    assert reply.status_code == 409
    assert reply.json() == {
        "error": "ssh_not_connected",
        "detail": f"SSH session '{session_id}' was closed ({CLOSED_BY_USER})",
    }
    assert ssh.provider.open_channels == 0
    await asyncio.sleep(0.05)
    assert fake_worker.sessions == set()


async def test_lost_worker_connection_closes_session(ssh, fake_worker, connect_body, events):
    session_id = (await open_session(ssh, connect_body))["session_id"]
    fake_worker.close_all()  # воркер оборвал все подключения
    for _ in range(50):
        if not len(ssh.manager):
            break
        await asyncio.sleep(0.02)
    assert (await ssh.api.get("/api/ssh/sessions")).json() == []
    await asyncio.sleep(0.05)
    assert events[-1] == {
        "action": "closed",
        "session_id": session_id,
        "host": "10.0.0.1",
        "port": 22,
        "username": "root",
        "reason": CLOSED_WORKER_LOST,
    }


async def test_worker_exit_closes_all_sessions(ssh, connect_body):
    await open_session(ssh, connect_body)
    await open_session(ssh, connect_body)
    await ssh.manager.on_worker_status(
        {"event_type": WORKER_STATUS_EVENT, "payload": {"status": "connected"}}
    )
    assert len(ssh.manager) == 2
    await ssh.manager.on_worker_status(
        {"event_type": WORKER_STATUS_EVENT, "payload": {"status": STATUS_WORKER_EXITED}}
    )
    assert len(ssh.manager) == 0
    assert ssh.provider.open_channels == 0
