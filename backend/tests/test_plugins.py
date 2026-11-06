"""Менеджер плагинов: зависимости, порядок загрузки, сбои отдельных плагинов."""

from __future__ import annotations

import sys
import textwrap
import uuid
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI

from app.plugins.manager import PluginManager, PluginStatus

# Плагины для тестов: имя -> (requires, код on_load). Пакет создаётся во временной папке.
PLUGIN_TEMPLATE = """
from fastapi import APIRouter

import {package} as journal
from app.plugins.base import BasePlugin

router = APIRouter()


@router.get("/ping")
async def ping():
    return {{"plugin": {name!r}}}


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
