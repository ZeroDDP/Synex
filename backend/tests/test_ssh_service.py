"""Служебные команды плагинов в SSH-сессии (SSHSessionManager.run_service)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.plugins.ssh.errors import (
    SSHNotConnectedError,
    SSHSessionBusyError,
    SSHSessionNotFoundError,
)
from tests.conftest import needs_timeout_utility
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


@pytest.fixture
async def shell_ssh(shell_worker, missing_socket_path):
    client, harness = await make_api(shell_worker.port, missing_socket_path)
    async with client:
        harness.api = client
        yield harness
    await harness.disconnect()


async def open_session(harness, body: dict) -> str:
    response = await harness.api.post("/api/ssh/sessions", json=body)
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


async def test_service_command_is_hidden_from_terminal_and_keeps_cwd(
    shell_ssh, shell_worker, connect_body
):
    session_id = await open_session(shell_ssh, connect_body)
    moved = await shell_ssh.api.post(
        f"/api/ssh/sessions/{session_id}/exec", json={"command": "cd /tmp && pwd"}
    )
    assert moved.json()["cwd"] == "/tmp"
    session = shell_ssh.manager.get(session_id)
    seq = session.buffer.last_seq

    result = await shell_ssh.manager.run_service(
        session_id, "pwd; echo problem >&2; exit 3", name="test"
    )
    assert result.stdout == shell_worker.shell_home  # из домашней папки
    assert result.stderr == "problem"
    assert result.exit_code == 3
    assert result.timed_out is False
    assert result.duration_ms >= 0

    assert session.buffer.last_seq == seq  # в терминал ничего не попало
    assert session.cwd == "/tmp"  # cwd сессии не поменялась


@needs_timeout_utility
async def test_service_command_is_limited_on_server(shell_ssh, connect_body):
    session_id = await open_session(shell_ssh, connect_body)
    result = await shell_ssh.manager.run_service(session_id, "echo begin; sleep 5", timeout=1)
    assert result.timed_out is True
    assert result.exit_code == 124
    assert result.stdout == "begin"


async def test_busy_session_is_reported_after_wait(ssh, connect_body):
    session_id = await open_session(ssh, connect_body)
    command = ssh.manager.execute(session_id, "sleep 1", track_cwd=False)
    user = asyncio.create_task(command)
    await asyncio.sleep(0.1)

    with pytest.raises(SSHSessionBusyError) as busy:
        await ssh.manager.run_service(session_id, "docker ps", wait=0.2, name="docker")
    assert "open a separate session for docker" in busy.value.detail
    assert busy.value.status_code == 409

    # С запасом ожидания служебная команда выполняется сразу после команды пользователя
    result = await ssh.manager.run_service(session_id, "docker ps", wait=5)
    assert user.done() and (await user).stdout == "ran: sleep 1"
    assert result.exit_code == 0


async def test_unknown_and_lost_sessions(ssh, fake_worker, connect_body):
    with pytest.raises(SSHSessionNotFoundError):
        await ssh.manager.run_service("0123456789abcdef", "true")

    session_id = await open_session(ssh, connect_body)
    fake_worker.drop_sessions()  # воркер перезапустили: сессии в нём больше нет
    with pytest.raises(SSHNotConnectedError):
        await ssh.manager.run_service(session_id, "true")
    with pytest.raises(SSHSessionNotFoundError):
        ssh.manager.get(session_id)
