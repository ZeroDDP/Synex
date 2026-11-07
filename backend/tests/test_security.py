"""Доступ к API только с этой машины: Origin и Host для HTTP и WebSocket."""

from __future__ import annotations

import sys

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from app.core.app import create_app
from app.core.config import settings
from app.core.security import allowed_hosts, allowed_origins, host_name
from app.worker import worker_manager
from tests.conftest import FAKE_WORKER_SCRIPT
from tests.ssh_helpers import recv_json, serve

EVIL = "https://evil.example"


@pytest.fixture
def app_settings(monkeypatch, worker_settings):
    monkeypatch.setattr(
        worker_manager, "_worker_command", lambda: [sys.executable, str(FAKE_WORKER_SCRIPT)]
    )
    monkeypatch.setattr(settings, "ALLOWED_ORIGINS", "")
    monkeypatch.setattr(settings, "UI_DEV_SERVER", True)
    return settings


@pytest.fixture
async def api(app_settings):
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            yield client


def test_allowed_lists(monkeypatch):
    monkeypatch.setattr(settings, "PORT", 8000)
    monkeypatch.setattr(settings, "ALLOWED_ORIGINS", " http://localhost:5173/ , tauri://localhost")
    assert allowed_origins(settings) == [
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://[::1]:8000",
        "http://localhost:5173",  # dev-сервер UI разрешён по умолчанию
        "http://127.0.0.1:5173",
        "tauri://localhost",
    ]
    monkeypatch.setattr(settings, "UI_DEV_SERVER", False)
    monkeypatch.setattr(settings, "ALLOWED_ORIGINS", "")
    assert allowed_origins(settings) == [
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://[::1]:8000",
    ]
    monkeypatch.setattr(settings, "HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "ALLOWED_HOSTS", "synex.local")
    assert allowed_hosts(settings) == {"127.0.0.1", "localhost", "::1", "synex.local"}
    # HOST=0.0.0.0 без ALLOWED_HOSTS: имена клиентов заранее неизвестны, проверки нет
    monkeypatch.setattr(settings, "HOST", "0.0.0.0")
    monkeypatch.setattr(settings, "ALLOWED_HOSTS", "")
    assert allowed_hosts(settings) is None


@pytest.mark.parametrize(
    ("header", "name"),
    [
        ("127.0.0.1:8000", "127.0.0.1"),
        ("LOCALHOST", "localhost"),
        ("[::1]:8000", "::1"),
        ("evil.example:8000", "evil.example"),
    ],
)
def test_host_name(header, name):
    assert host_name(header) == name


async def test_requests_without_origin_pass(api):
    """curl, PowerShell и настольное приложение не присылают Origin."""
    assert (await api.get("/api/health")).status_code == 200


async def test_foreign_web_page_is_rejected(api):
    for method, path in [("GET", "/api/ssh/sessions"), ("GET", "/api/keys")]:
        response = await api.request(method, path, headers={"Origin": EVIL})
        assert response.status_code == 403, (method, path)
        assert response.json()["error"] == "forbidden_origin"
        assert "access-control-allow-origin" not in response.headers

    # Предварительный CORS-запрос перед POST тоже отклоняется
    preflight = await api.options(
        "/api/ssh/sessions",
        headers={"Origin": EVIL, "Access-Control-Request-Method": "POST"},
    )
    assert preflight.status_code == 403


async def test_allowed_web_page_gets_cors_headers(api):
    origin = "http://localhost:5173"
    response = await api.get("/api/health", headers={"Origin": origin})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin

    preflight = await api.options(
        "/api/ssh/sessions",
        headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == origin


async def test_dns_rebinding_host_is_rejected(api):
    response = await api.get("/api/health", headers={"Host": "evil.example:8000"})
    assert response.status_code == 403
    assert response.json()["error"] == "forbidden_host"
    for host in ("localhost:8000", "127.0.0.1", "[::1]:8000"):
        assert (await api.get("/api/health", headers={"Host": host})).status_code == 200


async def test_websockets_check_origin(app_settings):
    """WebSocket не подчиняется CORS: без проверки Origin любая страница читала бы
    метрики и управляла терминалом."""
    async with serve(create_app()) as base:
        for path in ("/ws", "/api/ssh/sessions/0123456789abcdef/ws"):
            with pytest.raises(InvalidStatus) as excinfo:
                async with connect(base + path, origin=EVIL):
                    pass
            assert excinfo.value.response.status_code == 403, path

        async with connect(base + "/ws", origin="http://localhost:5173") as ws:
            assert (await recv_json(ws))["type"] == "hello"
        async with connect(base + "/ws") as ws:  # без Origin
            assert (await recv_json(ws))["type"] == "hello"


async def test_ui_dev_server_works_without_settings(app_settings, monkeypatch):
    """`npm run dev` в frontend/: http://localhost:5173 и http://127.0.0.1:5173 пускаются
    без ALLOWED_ORIGINS, а с UI_DEV_SERVER=false уже нет."""
    async with serve(create_app()) as base:
        for origin in ("http://localhost:5173", "http://127.0.0.1:5173"):
            async with connect(base + "/ws", origin=origin) as ws:
                assert (await recv_json(ws))["type"] == "hello"

    monkeypatch.setattr(settings, "UI_DEV_SERVER", False)
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as api:
            response = await api.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert response.status_code == 403
    assert "port 5173" in response.json()["detail"]
