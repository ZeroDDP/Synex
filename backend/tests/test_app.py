"""Приложение целиком: lifespan, плагины, воркер как subprocess, HTTP API."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

from app.core.app import create_app
from app.plugins.ssh.manager import ssh_manager
from app.worker import worker_manager
from tests.conftest import FAKE_WORKER_SCRIPT


@pytest.fixture
async def api(monkeypatch, worker_settings):
    monkeypatch.setattr(
        worker_manager, "_worker_command", lambda: [sys.executable, str(FAKE_WORKER_SCRIPT)]
    )
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            yield client
    await ssh_manager.close_all(reason="test")


async def test_only_ssh_api_is_exposed():
    paths = set(create_app().openapi()["paths"])
    assert paths == {
        "/api/external-plugins",
        "/api/external-plugins/inspect",
        "/api/external-plugins/install",
        "/api/external-plugins/{plugin}",
        "/api/external-plugins/{plugin}/enable",
        "/api/external-plugins/{plugin}/disable",
        "/api/external-plugins/{plugin}/update",
        "/api/external-plugins/{plugin}/rollback",
        "/api/health",
        "/api/plugins",
        "/api/plugins/{name}/enable",
        "/api/plugins/{name}/disable",
        "/api/ssh/connect",
        "/api/ssh/exec",
        "/api/ssh/disconnect",
        "/api/ssh/status",
        "/api/ssh/known-hosts",
        "/api/ssh/sessions",
        "/api/ssh/sessions/{session_id}",
        "/api/ssh/sessions/{session_id}/exec",
        "/api/ssh/sessions/{session_id}/interrupt",
        "/api/ssh/sessions/{session_id}/history",
        "/api/ssh/sessions/{session_id}/complete",
        "/api/ssh/sessions/{session_id}/output",
        "/api/monitor/metrics",
        "/api/keys",
        "/api/keys/generate",
        "/api/keys/{key_id}",
    }


async def test_health_reports_worker(api):
    body = (await api.get("/api/health")).json()
    assert body["status"] == "ok"
    assert body["plugins"] == ["ssh", "keys", "monitor"]
    assert body["worker"]["connected"] is True
    assert body["worker"]["worker_pid"]


async def test_ssh_connect_through_worker_subprocess(api, tmp_path: Path):
    key = tmp_path / "id_ed25519"
    key.write_text("key")
    body = {"host": "10.0.0.1", "username": "root", "key_path": str(key)}

    response = await api.post("/api/ssh/connect", json=body)
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "connected"
    session_id = response.json()["session_id"]

    status = (await api.get("/api/ssh/status")).json()
    assert status["connected"] is True and status["host"] == "10.0.0.1"
    assert status["session_id"] == session_id

    exec_response = await api.post("/api/ssh/exec", json={"command": "ls", "track_cwd": False})
    assert exec_response.json()["stdout"] == "ran: ls"

    closed = await api.post("/api/ssh/disconnect", json={"session_id": session_id})
    assert closed.status_code == 200, closed.text
    assert (await api.get("/api/ssh/status")).json()["connected"] is False


async def test_ssh_connect_with_generated_key(api):
    key = (await api.post("/api/keys/generate", json={"name": "server"})).json()
    assert key["public_key"].startswith("ssh-ed25519 ")

    body = {"host": "10.0.0.1", "username": "root", "key_id": key["id"]}
    response = await api.post("/api/ssh/connect", json=body)
    assert response.status_code == 201, response.text
    assert response.json()["key_id"] == key["id"]


async def test_worker_is_stopped_after_shutdown(monkeypatch, worker_settings):
    monkeypatch.setattr(
        worker_manager, "_worker_command", lambda: [sys.executable, str(FAKE_WORKER_SCRIPT)]
    )
    app = create_app()
    async with app.router.lifespan_context(app):
        process = worker_manager._process
        assert process is not None and process.poll() is None
    assert process.poll() is not None
    assert worker_manager.client is None


async def test_each_session_gets_its_own_worker_connection(api, tmp_path: Path):
    """Весь путь: менеджер сессий берёт отдельное подключение у WorkerManager."""
    key = tmp_path / "id_ed25519"
    key.write_text("key")
    body = {"host": "10.0.0.1", "username": "root", "key_path": str(key)}

    first = (await api.post("/api/ssh/sessions", json=body)).json()
    second = (await api.post("/api/ssh/sessions", json=body)).json()
    assert first["channel"] == second["channel"] == "dedicated"
    assert (await api.get("/api/health")).json()["worker"]["channels"] == 2

    await api.delete(f"/api/ssh/sessions/{first['session_id']}")
    assert (await api.get("/api/health")).json()["worker"]["channels"] == 1
