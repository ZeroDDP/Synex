"""Набор плагинов переживает перезапуск backend: что пользователь включил и выключил на
ходу, запоминается в plugins.json (app/plugins/choices.py) и при старте накладывается на
PLUGINS_ENABLED."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.api.router import API_PREFIX, build_api_router
from app.core.app import create_app
from app.core.config import settings
from app.core.errors import register_error_handlers
from app.plugins.choices import PluginChoices
from app.plugins.manager import PluginManager
from app.ws import WebSocketHub

PLUGINS = {
    "ssh": ((), ""),
    "sftp": (("ssh",), ""),
    "docker": (("ssh",), ""),
    "keys": ((), ""),
    "extra": ((), ""),
}


@pytest.fixture
def saved(tmp_path: Path) -> Path:
    return tmp_path / "data" / "plugins.json"


async def start(package: str, enabled: list[str], saved: Path | None) -> FastAPI:
    """Запуск backend: новый менеджер плагинов с тем же файлом выбора."""
    app = FastAPI()
    register_error_handlers(app)
    api_router = build_api_router()
    choices = PluginChoices(saved) if saved is not None else None
    manager = PluginManager(package, forwarder=WebSocketHub(), choices=choices)
    manager.discover(api_router, enabled=enabled)
    app.include_router(api_router)
    manager.attach(app, API_PREFIX)
    app.state.plugins = manager
    await manager.load_all()
    return app


def api(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def statuses(app: FastAPI) -> dict[str, str]:
    async with api(app) as client:
        return {p["name"]: p["status"] for p in (await client.get("/api/plugins")).json()}


def stored(saved: Path) -> dict[str, bool]:
    return json.loads(saved.read_text(encoding="utf-8"))["plugins"]


async def test_switched_plugins_stay_switched_after_restart(make_plugins, saved: Path):
    package = make_plugins(PLUGINS)
    configured = ["ssh", "sftp", "docker", "keys"]  # extra установлен, но не включён
    app = await start(package, configured, saved)
    assert not saved.exists()  # пока пользователь ничего не переключал, файла нет
    async with api(app) as client:
        assert (await client.post("/api/plugins/docker/disable")).status_code == 200
        assert (await client.post("/api/plugins/extra/enable")).status_code == 200
    assert stored(saved) == {"docker": False, "extra": True}
    if os.name != "nt":
        assert saved.stat().st_mode & 0o077 == 0

    # Перезапуск: те же настройки, набор тот, что оставил пользователь
    app = await start(package, configured, saved)
    assert await statuses(app) == {
        "ssh": "loaded",
        "sftp": "loaded",
        "keys": "loaded",
        "extra": "loaded",
        "docker": "disabled",
    }
    async with api(app) as client:
        assert (await client.get("/api/docker/ping")).status_code == 404
        assert (await client.get("/api/extra/ping")).json() == {"plugin": "extra"}
        assert "/api/docker/ping" not in app.openapi()["paths"]

        # Вернул как было в настройках: записи о плагинах пропадают
        assert (await client.post("/api/plugins/docker/enable")).json()["changed"] == ["docker"]
        assert (await client.get("/api/docker/ping")).json() == {"plugin": "docker"}
        assert stored(saved) == {"extra": True}
        assert (await client.post("/api/plugins/extra/disable")).status_code == 200
    assert stored(saved) == {}

    app = await start(package, configured, saved)
    assert await statuses(app) == {
        "ssh": "loaded",
        "sftp": "loaded",
        "docker": "loaded",
        "keys": "loaded",
        "extra": "disabled",
    }


async def test_cascade_and_dependencies_are_remembered(make_plugins, saved: Path):
    package = make_plugins(PLUGINS)
    everything = ["ssh", "sftp", "docker", "keys", "extra"]
    app = await start(package, everything, saved)
    async with api(app) as client:
        off = await client.post("/api/plugins/ssh/disable", params={"cascade": "true"})
        assert sorted(off.json()["changed"]) == ["docker", "sftp", "ssh"]
    assert stored(saved) == {"docker": False, "sftp": False, "ssh": False}

    app = await start(package, everything, saved)
    assert await statuses(app) == {
        "keys": "loaded",
        "extra": "loaded",
        "ssh": "disabled",
        "sftp": "disabled",
        "docker": "disabled",
    }
    async with api(app) as client:
        # Включение плагина включает его зависимость: запоминаются оба
        on = await client.post("/api/plugins/sftp/enable")
        assert on.json()["changed"] == ["ssh", "sftp"]
    assert stored(saved) == {"docker": False}

    app = await start(package, everything, saved)
    states = await statuses(app)
    assert (states["ssh"], states["sftp"], states["docker"]) == ("loaded", "loaded", "disabled")


async def test_settings_still_decide_what_the_user_did_not_touch(make_plugins, saved: Path):
    package = make_plugins(PLUGINS)
    app = await start(package, ["ssh", "sftp", "docker", "keys"], saved)
    async with api(app) as client:
        await client.post("/api/plugins/docker/disable")

    # PLUGINS_ENABLED изменили: keys убран, extra добавлен. docker выключал пользователь
    app = await start(package, ["ssh", "sftp", "docker", "extra"], saved)
    assert await statuses(app) == {
        "ssh": "loaded",
        "sftp": "loaded",
        "extra": "loaded",
        "docker": "disabled",
        "keys": "disabled",
    }

    # Плагин, который нужен включённому, включается как зависимость, даже если раньше
    # его выключали: иначе включённый не заработал бы
    saved.write_text(json.dumps({"version": 1, "plugins": {"ssh": False}}), encoding="utf-8")
    app = await start(package, ["ssh", "sftp"], saved)
    states = await statuses(app)
    assert (states["ssh"], states["sftp"]) == ("loaded", "loaded")


async def test_a_plugin_that_did_not_start_is_remembered_as_enabled(make_plugins, saved: Path):
    """Пользователь включил плагин, а тот не запустился: после перезапуска backend
    пробует снова (и снова показывает причину), а не делает вид, что плагин выключен."""
    package = make_plugins({"good": ((), ""), "flaky": ((), "raise RuntimeError('no docker')")})
    app = await start(package, ["good"], saved)
    async with api(app) as client:
        failed = await client.post("/api/plugins/flaky/enable")
        assert (failed.status_code, failed.json()["error"]) == (503, "plugin_unavailable")
        missing = await client.post("/api/plugins/nothere/enable")
        assert missing.status_code == 404
    assert stored(saved) == {"flaky": True}

    app = await start(package, ["good"], saved)
    async with api(app) as client:
        plugins = {p["name"]: p for p in (await client.get("/api/plugins")).json()}
    assert (plugins["good"]["status"], plugins["flaky"]["status"]) == ("loaded", "failed")
    assert "no docker" in plugins["flaky"]["error"]


async def test_a_remembered_plugin_that_is_gone_is_skipped(make_plugins, saved: Path):
    package = make_plugins({"good": ((), "")})
    saved.parent.mkdir(parents=True)
    saved.write_text(
        json.dumps({"version": 1, "plugins": {"removed": True, "good": False}}), encoding="utf-8"
    )
    app = await start(package, ["good"], saved)
    async with api(app) as client:
        plugins = {p["name"]: p for p in (await client.get("/api/plugins")).json()}
    assert plugins["good"]["status"] == "disabled"
    assert (plugins["removed"]["status"], plugins["removed"]["error"]) == (
        "skipped",
        "not installed",
    )


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"version": 2, "plugins": {"docker": false}}',
        '{"version": 1, "plugins": ["docker"]}',
        '{"version": 1}',
    ],
)
async def test_a_damaged_file_is_ignored_and_rewritten(make_plugins, saved: Path, content: str):
    package = make_plugins(PLUGINS)
    saved.parent.mkdir(parents=True)
    saved.write_text(content, encoding="utf-8")
    app = await start(package, ["ssh", "docker"], saved)
    assert (await statuses(app))["docker"] == "loaded"  # работает набор из настроек
    async with api(app) as client:
        assert (await client.post("/api/plugins/docker/disable")).status_code == 200
    assert stored(saved) == {"docker": False}


def test_strange_entries_are_dropped(saved: Path):
    saved.parent.mkdir(parents=True)
    saved.write_text(
        json.dumps(
            {
                "version": 1,
                "plugins": {"docker": False, "git": True, "Bad-Name": True, "x": "yes", "y": 1},
            }
        ),
        encoding="utf-8",
    )
    choices = PluginChoices(saved)
    assert choices.read() == {"docker": False, "git": True}
    choices.remember({"docker": None, "sftp": False, "missing": None})
    assert choices.read() == {"git": True, "sftp": False}
    # Нечего менять: файл не переписывается
    before = saved.stat().st_mtime_ns
    choices.remember({"git": True, "docker": None})
    assert saved.stat().st_mtime_ns == before


async def test_a_file_that_cannot_be_written_does_not_break_switching(make_plugins, tmp_path: Path):
    package = make_plugins(PLUGINS)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the data folder should be")
    saved = blocker / "plugins.json"  # папку создать нельзя: на её месте файл
    app = await start(package, ["ssh", "docker"], saved)
    async with api(app) as client:
        off = await client.post("/api/plugins/docker/disable")
        assert (off.status_code, off.json()["changed"]) == (200, ["docker"])
    assert (await statuses(app))["docker"] == "disabled"


async def test_without_remembering_settings_decide(make_plugins, saved: Path):
    package = make_plugins(PLUGINS)
    app = await start(package, ["ssh", "docker"], None)
    async with api(app) as client:
        assert (await client.post("/api/plugins/docker/disable")).status_code == 200
    assert not saved.exists()
    app = await start(package, ["ssh", "docker"], None)
    assert (await statuses(app))["docker"] == "loaded"


def test_the_application_reads_the_choices(monkeypatch):
    """create_app: выбор лежит в PLUGINS_STATE_PATH, PLUGINS_REMEMBER=false его не читает."""
    monkeypatch.setattr(settings, "PLUGINS_ENABLED", "*")
    path = settings.plugins_state_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "plugins": {"docker": False, "git": False}}), encoding="utf-8"
    )

    def plugin_states() -> dict[str, str]:
        return {state.name: str(state.status) for state in create_app().state.plugins.states()}

    states = plugin_states()
    assert (states["docker"], states["git"]) == ("disabled", "disabled")
    assert (states["ssh"], states["sftp"], states["keys"]) == ("registered",) * 3

    monkeypatch.setattr(settings, "PLUGINS_REMEMBER", False)
    assert plugin_states()["docker"] == "registered"

    # Путь из настроек: относительный считается от backend/
    monkeypatch.setattr(settings, "PLUGINS_STATE_PATH", Path("data/plugins.json"))
    assert settings.plugins_state_path == settings.BASE_DIR / "data" / "plugins.json"
