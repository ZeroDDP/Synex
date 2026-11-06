"""Менеджер плагинов: зависимости, порядок загрузки, сбои отдельных плагинов."""

from __future__ import annotations

import sys
import textwrap
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from app.api.router import build_api_router
from app.core.config import Settings
from app.core.errors import register_error_handlers
from app.plugins.manager import PluginManager, PluginStatus
from tests.ssh_helpers import recv_json, serve

# Плагины для тестов: имя -> (requires, код on_load). Пакет создаётся во временной папке.
PLUGIN_TEMPLATE = """
from fastapi import APIRouter, WebSocket

import {package} as journal
from app.plugins.base import BasePlugin

router = APIRouter()


@router.get("/ping")
async def ping():
    return {{"plugin": {name!r}}}


@router.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_json({{"plugin": {name!r}}})
    await websocket.close()


class TestPlugin(BasePlugin):
    name = {name!r}
    version = "1.0.0"
    description = "Plugin {name}"
    requires = {requires!r}
    router = router

    async def on_load(self):
        {on_load}
        journal.events.append(("load", {name!r}))

    async def on_unload(self):
        journal.events.append(("unload", {name!r}))
"""


@pytest.fixture
def make_plugins(tmp_path: Path, monkeypatch):
    """make_plugins({name: (requires, on_load)}) -> имя пакета с этими плагинами."""
    package = f"synex_test_plugins_{uuid.uuid4().hex[:8]}"
    root = tmp_path / package
    root.mkdir()
    (root / "__init__.py").write_text("events = []  # on_load/on_unload всех плагинов\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    def make(plugins: dict[str, tuple[tuple[str, ...], str]]) -> str:
        for name, (requires, on_load) in plugins.items():
            directory = root / name
            directory.mkdir()
            (directory / "__init__.py").write_text("")
            code = PLUGIN_TEMPLATE.format(
                package=package, name=name, requires=requires, on_load=on_load or "pass"
            )
            (directory / "plugin.py").write_text(textwrap.dedent(code))
        return package

    yield make
    for module in [m for m in sys.modules if m.startswith(package)]:
        del sys.modules[module]


def events(package: str) -> list[tuple[str, str]]:
    """Журнал on_load/on_unload всех плагинов пакета, по порядку вызова."""
    return sys.modules[package].events


def paths(router: APIRouter) -> set[str]:
    app = FastAPI()
    app.include_router(router)
    return set(app.openapi()["paths"])


async def test_dependencies_load_first_and_are_enabled_automatically(make_plugins):
    package = make_plugins(
        {
            "base": ((), ""),
            "docker": (("base",), ""),
            "deploy": (("docker", "base"), ""),
        }
    )
    manager = PluginManager(package)
    router = APIRouter()
    manager.discover(router, enabled=["deploy"])  # base и docker включаются сами

    assert manager.names == ["base", "docker", "deploy"]
    assert paths(router) == {"/base/ping", "/docker/ping", "/deploy/ping"}
    assert all(manager.status(n) == PluginStatus.REGISTERED for n in manager.names)

    await manager.load_all()
    await manager.unload_all()
    assert events(package) == [
        ("load", "base"), ("load", "docker"), ("load", "deploy"),
        ("unload", "deploy"), ("unload", "docker"), ("unload", "base"),
    ]  # fmt: skip
    assert manager.loaded == []


async def test_enabled_order_is_kept_for_independent_plugins(make_plugins):
    package = make_plugins({"b": ((), ""), "a": ((), ""), "c": (("a",), "")})
    manager = PluginManager(package)
    manager.discover(APIRouter(), enabled=["c", "b", "a"])
    assert manager.names == ["b", "a", "c"]


async def test_missing_and_cyclic_dependencies_are_skipped(make_plugins):
    package = make_plugins(
        {
            "ok": ((), ""),
            "orphan": (("nothere",), ""),
            "chain": (("orphan",), ""),
            "ping": (("pong",), ""),
            "pong": (("ping",), ""),
        }
    )
    manager = PluginManager(package)
    router = APIRouter()
    manager.discover(router, enabled=["ok", "chain", "ping"])

    assert manager.names == ["ok"]
    assert paths(router) == {"/ok/ping"}
    states = {name: manager.state(name) for name in ("nothere", "orphan", "chain", "ping", "pong")}
    assert all(s is not None and s.status == PluginStatus.SKIPPED for s in states.values())
    assert states["nothere"].error == "not installed"
    assert states["orphan"].error == "requires plugin 'nothere', which is not installed"
    assert states["chain"].error == "requires plugin 'orphan', which is skipped"
    assert "dependency cycle" in states["ping"].error


async def test_broken_plugin_does_not_stop_others(make_plugins):
    package = make_plugins(
        {
            "good": ((), ""),
            "broken": ((), "raise RuntimeError('no docker here')"),
            "user": (("broken",), ""),
            "after": ((), ""),
        }
    )
    manager = PluginManager(package)
    manager.discover(APIRouter(), enabled=["good", "user", "after"])
    await manager.load_all()

    assert manager.loaded == ["good", "after"]
    broken, user = manager.state("broken"), manager.state("user")
    assert broken.status == PluginStatus.FAILED
    assert broken.error == "on_load failed: RuntimeError: no docker here"
    assert user.status == PluginStatus.FAILED
    assert user.error == "required plugin 'broken' is not loaded"

    await manager.unload_all()  # упавшие не выгружаются
    assert ("unload", "broken") not in events(package)


async def test_import_error_is_reported(make_plugins, tmp_path):
    package = make_plugins({"fine": ((), "")})
    bad = tmp_path / package / "bad"
    bad.mkdir()
    (bad / "__init__.py").write_text("")
    (bad / "plugin.py").write_text("import module_that_does_not_exist\n")
    empty = tmp_path / package / "empty"
    empty.mkdir()
    (empty / "__init__.py").write_text("")
    (empty / "plugin.py").write_text("X = 1\n")

    manager = PluginManager(package)
    manager.discover(APIRouter(), enabled=["bad", "empty", "fine"])
    assert manager.names == ["fine"]
    assert manager.state("bad").error.startswith("import failed: ModuleNotFoundError")
    assert manager.state("empty").error == "no BasePlugin subclass in plugin.py"


def test_builtin_plugins_declare_dependencies():
    manager = PluginManager()
    manager.discover(APIRouter(), enabled=["monitor"])
    assert manager.names == ["ssh", "monitor"]
    for plugin in manager.plugins.values():
        assert plugin.description


async def build_app(package: str, enabled: list[str]) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    api_router = build_api_router()
    app.state.plugins = PluginManager(package)
    app.state.plugins.discover(api_router, enabled=enabled)
    app.include_router(api_router)
    await app.state.plugins.load_all()
    return app


async def test_plugins_api_and_unavailable_plugin(make_plugins):
    package = make_plugins(
        {
            "good": ((), ""),
            "broken": ((), "raise RuntimeError('no docker here')"),
            "user": (("broken",), ""),
        }
    )
    app = await build_app(package, ["good", "user", "missing"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        plugins = (await api.get("/api/plugins")).json()
        assert plugins == [
            {
                "name": "good",
                "version": "1.0.0",
                "description": "Plugin good",
                "requires": [],
                "status": "loaded",
                "error": None,
            },
            {
                "name": "broken",
                "version": "1.0.0",
                "description": "Plugin broken",
                "requires": [],
                "status": "failed",
                "error": "on_load failed: RuntimeError: no docker here",
            },
            {
                "name": "user",
                "version": "1.0.0",
                "description": "Plugin user",
                "requires": ["broken"],
                "status": "failed",
                "error": "required plugin 'broken' is not loaded",
            },
            {
                "name": "missing",
                "version": None,
                "description": "",
                "requires": [],
                "status": "skipped",
                "error": "not installed",
            },
        ]

        assert (await api.get("/api/good/ping")).json() == {"plugin": "good"}
        response = await api.get("/api/broken/ping")
        assert response.status_code == 503
        assert response.json() == {
            "error": "plugin_unavailable",
            "detail": "Plugin 'broken' is not available: on_load failed: RuntimeError: "
            "no docker here",
        }
        assert (await api.get("/api/user/ping")).json()["error"] == "plugin_unavailable"
        assert (await api.get("/api/health")).json()["plugins"] == ["good"]


async def test_websocket_of_unavailable_plugin_is_rejected(make_plugins):
    package = make_plugins({"good": ((), ""), "broken": ((), "raise RuntimeError('boom')")})
    app = await build_app(package, ["good", "broken"])
    async with serve(app) as base:
        async with connect(f"{base}/api/good/ws") as ws:
            assert await recv_json(ws) == {"plugin": "good"}
        with pytest.raises(InvalidStatus) as rejected:
            await connect(f"{base}/api/broken/ws")
        # Рукопожатие отклоняется тем же ответом 503, что и обычный запрос
        assert rejected.value.response.status_code == 503
        assert b"plugin_unavailable" in rejected.value.response.body


async def test_star_enables_all_installed_plugins(make_plugins, tmp_path):
    package = make_plugins({"b": (("a",), ""), "a": ((), ""), "c": ((), "")})
    (tmp_path / package / "not_a_plugin").mkdir()  # папка без plugin.py
    manager = PluginManager(package)
    assert manager.installed() == ["a", "b", "c"]
    manager.discover(APIRouter(), enabled=["*"])
    assert manager.names == ["a", "b", "c"]


async def test_installed_but_not_enabled_plugins_are_listed(make_plugins):
    package = make_plugins({"on": ((), ""), "off": ((), "")})
    app = await build_app(package, ["on"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        plugins = (await api.get("/api/plugins")).json()
        assert (await api.get("/api/off/ping")).status_code == 404
    assert [(p["name"], p["status"]) for p in plugins] == [("on", "loaded"), ("off", "disabled")]


def test_default_enables_every_builtin_plugin():
    assert Settings.model_fields["PLUGINS_ENABLED"].default == "*"
    manager = PluginManager()
    manager.discover(APIRouter(), enabled=["*"])
    assert set(manager.names) == set(manager.installed()) >= {"ssh", "keys", "monitor"}
    assert manager.names.index("ssh") < manager.names.index("monitor")
