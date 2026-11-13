"""Шина данных: плагины включаются и выключаются без перезапуска backend (SYN-70)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.api.router import API_PREFIX, build_api_router
from app.core.app import create_app
from app.core.config import settings
from app.core.errors import register_error_handlers
from app.core.event_bus import event_bus
from app.plugins.manager import PLUGIN_STATUS_EVENT, PluginManager
from app.worker import worker_manager
from app.ws import WebSocketHub
from tests.conftest import FAKE_WORKER_SCRIPT
from tests.plugin_package import events
from tests.ssh_helpers import recv_json, serve


async def build_app(package: str, enabled: list[str], hub: WebSocketHub) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    api_router = build_api_router()
    manager = PluginManager(package, forwarder=hub)
    manager.discover(api_router, enabled=enabled)
    app.include_router(api_router)
    manager.attach(app, API_PREFIX)
    app.state.plugins = manager
    await manager.load_all()
    return app


@pytest.fixture
def announced() -> list[dict]:
    received: list[dict] = []

    async def collect(event: dict) -> None:
        received.append(event)

    event_bus.subscribe(PLUGIN_STATUS_EVENT, collect)
    yield received
    event_bus.unsubscribe(PLUGIN_STATUS_EVENT, collect)


def api(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def statuses(body: dict) -> dict[str, str]:
    return {p["name"]: p["status"] for p in body["plugins"]}


async def test_disable_removes_routes_and_enable_brings_them_back(make_plugins, announced):
    package = make_plugins({"files": ((), "")})
    app = await build_app(package, ["files"], WebSocketHub())
    async with api(app) as client:
        assert (await client.get("/api/files/ping")).json() == {"plugin": "files"}

        off = await client.post("/api/plugins/files/disable")
        assert off.status_code == 200, off.text
        assert off.json()["changed"] == ["files"]
        assert statuses(off.json())["files"] == "disabled"
        gone = await client.get("/api/files/ping")
        assert gone.status_code == 404
        assert "/api/files/ping" not in app.openapi()["paths"]

        on = await client.post("/api/plugins/files/enable")
        assert on.status_code == 200, on.text
        assert (on.json()["changed"], statuses(on.json())["files"]) == (["files"], "loaded")
        assert (await client.get("/api/files/ping")).json() == {"plugin": "files"}
        assert "/api/files/ping" in app.openapi()["paths"]

        # Повторы ничего не меняют
        assert (await client.post("/api/plugins/files/enable")).json()["changed"] == []
    assert events(package) == [("load", "files"), ("unload", "files"), ("load", "files")]
    assert [(e["name"], e["status"]) for e in announced] == [
        ("files", "loaded"),
        ("files", "disabled"),
        ("files", "loaded"),
    ]


async def test_dependents_block_disable_unless_cascade(make_plugins):
    package = make_plugins(
        {"ssh": ((), ""), "sftp": (("ssh",), ""), "docker": (("ssh",), ""), "other": ((), "")}
    )
    app = await build_app(package, ["sftp", "docker", "other"], WebSocketHub())
    async with api(app) as client:
        refused = await client.post("/api/plugins/ssh/disable")
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"] == "plugin_in_use"
        assert "sftp" in refused.json()["detail"] and "docker" in refused.json()["detail"]
        assert (await client.get("/api/ssh/ping")).status_code == 200

        cascade = await client.post("/api/plugins/ssh/disable", params={"cascade": "true"})
        assert cascade.status_code == 200, cascade.text
        assert sorted(cascade.json()["changed"]) == ["docker", "sftp", "ssh"]
        assert cascade.json()["changed"][-1] == "ssh"  # зависимость выключается последней
        for name in ("ssh", "sftp", "docker"):
            assert (await client.get(f"/api/{name}/ping")).status_code == 404
        assert (await client.get("/api/other/ping")).status_code == 200

        # Включение плагина включает и его зависимость
        on = await client.post("/api/plugins/sftp/enable")
        assert on.json()["changed"] == ["ssh", "sftp"]
        assert statuses(on.json())["docker"] == "disabled"
    loads = [e for e in events(package) if e[0] == "load"]
    assert loads[-2:] == [("load", "ssh"), ("load", "sftp")]


async def test_a_plugin_installed_while_running_is_loaded(make_plugins, tmp_path: Path):
    package = make_plugins({"core": ((), "")})
    app = await build_app(package, ["core"], WebSocketHub())
    async with api(app) as client:
        listed = {p["name"] for p in (await client.get("/api/plugins")).json()}
        assert listed == {"core"}
        make_plugins({"fresh": (("core",), "")})  # новый пакет появился после старта

        on = await client.post("/api/plugins/fresh/enable")
        assert on.status_code == 200, on.text
        assert statuses(on.json())["fresh"] == "loaded"
        assert (await client.get("/api/fresh/ping")).json() == {"plugin": "fresh"}

        missing = await client.post("/api/plugins/nothere/enable")
        assert (missing.status_code, missing.json()["error"]) == (404, "plugin_not_found")
        bad = await client.post("/api/plugins/Bad-Name/enable")
        assert bad.status_code == 422


async def test_failing_on_load_is_reported(make_plugins):
    package = make_plugins({"good": ((), ""), "flaky": ((), "raise RuntimeError('no docker')")})
    app = await build_app(package, ["good"], WebSocketHub())
    async with api(app) as client:
        failed = await client.post("/api/plugins/flaky/enable")
        assert (failed.status_code, failed.json()["error"]) == (503, "plugin_unavailable")
        assert "no docker" in failed.json()["detail"]
        plugins = {p["name"]: p for p in (await client.get("/api/plugins")).json()}
        assert (plugins["flaky"]["status"], plugins["good"]["status"]) == ("failed", "loaded")
        unavailable = await client.get("/api/flaky/ping")
        assert (unavailable.status_code, unavailable.json()["error"]) == (
            503,
            "plugin_unavailable",
        )
        # Упавший плагин можно выключить, и его роуты снимаются
        off = await client.post("/api/plugins/flaky/disable")
        assert off.status_code == 200
        assert (await client.get("/api/flaky/ping")).status_code == 404


async def test_websocket_routes_follow_the_plugin(make_plugins):
    package = make_plugins({"live": ((), "")})
    app = await build_app(package, ["live"], WebSocketHub())
    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus

    async with serve(app) as base:
        async with connect(f"{base}/api/live/ws") as ws:
            assert await recv_json(ws) == {"plugin": "live"}
        await app.state.plugins.disable("live")
        with pytest.raises(InvalidStatus):
            await connect(f"{base}/api/live/ws")
        await app.state.plugins.enable("live")
        async with connect(f"{base}/api/live/ws") as ws:
            assert await recv_json(ws) == {"plugin": "live"}


async def test_concurrent_switching_is_serialized(make_plugins):
    package = make_plugins({"a": ((), "")})
    app = await build_app(package, ["a"], WebSocketHub())
    manager: PluginManager = app.state.plugins
    routes = len(app.router.routes)
    await asyncio.gather(*(manager.disable("a") for _ in range(3)))
    await asyncio.gather(*(manager.enable("a") for _ in range(3)))
    assert events(package) == [("load", "a"), ("unload", "a"), ("load", "a")]
    assert len(app.router.routes) == routes  # роуты не задвоились
    async with api(app) as client:
        assert (await client.get("/api/a/ping")).json() == {"plugin": "a"}


# ----------------------------------------------------- the four core modules


@pytest.fixture
def real_app_settings(monkeypatch, worker_settings):
    monkeypatch.setattr(
        worker_manager, "_worker_command", lambda: [sys.executable, str(FAKE_WORKER_SCRIPT)]
    )
    monkeypatch.setattr(settings, "PLUGINS_ENABLED", "*")
    return settings


@pytest.fixture
def real_app(real_app_settings) -> FastAPI:
    # Запуск и остановку (lifespan) делает uvicorn в serve(): второй запуск поднял бы
    # второй воркер на том же адресе (на Windows фейковый воркер слушает TCP-порт)
    return create_app()


async def test_four_core_modules_switch_without_restart(real_app: FastAPI, tmp_path: Path):
    """SSH, SFTP, мониторинг и Docker: выключаются и включаются на работающем backend,
    UI узнаёт об этом из /ws (plugin.status), а их API пропадает и возвращается."""
    key = tmp_path / "id_ed25519"
    key.write_text("key")
    async with serve(real_app) as base:
        from websockets.asyncio.client import connect

        http = base.replace("ws://", "http://")
        async with (
            connect(f"{base}/ws") as ws,
            httpx.AsyncClient(base_url=http) as client,
        ):
            hello = await recv_json(ws)
            assert "plugin.status" in hello["data"]["streams"]
            opened = await client.post(
                "/api/ssh/sessions",
                json={"host": "10.0.0.1", "username": "root", "key_path": str(key)},
            )
            assert opened.status_code == 201, opened.text

            off = await client.post("/api/plugins/ssh/disable", params={"cascade": "true"})
            assert off.status_code == 200, off.text
            assert {"ssh", "sftp", "monitor", "docker"} <= set(off.json()["changed"])
            for path in ("/api/ssh/sessions", "/api/sftp/transfers", "/api/monitor/metrics"):
                assert (await client.get(path)).status_code == 404, path
            assert (await client.get("/api/keys")).status_code == 200  # не зависит от ssh

            # До UI доходят и смена состояния модулей, и то, что ssh сообщил при выгрузке
            seen: dict[str, str] = {}
            closed: list[dict] = []
            core = ("ssh", "sftp", "monitor", "docker")
            async with asyncio.timeout(10):
                while not closed or any(seen.get(n) != "disabled" for n in core):
                    message = await recv_json(ws)
                    if message["type"] == "plugin.status":
                        seen[message["data"]["name"]] = message["data"]["status"]
                    elif message["type"] == "ssh.session" and message["data"]["action"] == "closed":
                        closed.append(message["data"])
            assert [e["reason"] for e in closed] == ["shutdown"]

            for name in ("ssh", "sftp", "monitor", "docker"):
                on = await client.post(f"/api/plugins/{name}/enable")
                assert on.status_code == 200, on.text
            plugins = {p["name"]: p["status"] for p in (await client.get("/api/plugins")).json()}
            assert all(plugins[n] == "loaded" for n in ("ssh", "sftp", "monitor", "docker"))
            # Сессии закрылись вместе с ssh, а новая открывается
            assert (await client.get("/api/ssh/sessions")).json() == []
            again = await client.post(
                "/api/ssh/sessions",
                json={"host": "10.0.0.1", "username": "root", "key_path": str(key)},
            )
            assert again.status_code == 201, again.text
            assert (await client.get("/api/sftp/transfers")).status_code == 200
            assert (await client.get("/api/monitor/metrics")).status_code == 200
