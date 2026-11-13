"""Пакет плагинов для тестов менеджера плагинов: создаётся во временной папке.

Фикстура make_plugins подключена в conftest.py, поэтому доступна во всех тестах.
"""

from __future__ import annotations

import importlib
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

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
    # Кодировка явно: на Windows по умолчанию cp1252, и кириллица в файле не записалась бы
    journal = "events = []  # on_load/on_unload всех плагинов\n"
    (root / "__init__.py").write_text(journal, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    def make(plugins: dict[str, tuple[tuple[str, ...], str]]) -> str:
        for name, (requires, on_load) in plugins.items():
            directory = root / name
            directory.mkdir()
            (directory / "__init__.py").write_text("", encoding="utf-8")
            code = PLUGIN_TEMPLATE.format(
                package=package, name=name, requires=requires, on_load=on_load or "pass"
            )
            (directory / "plugin.py").write_text(textwrap.dedent(code), encoding="utf-8")
        importlib.invalidate_caches()  # пакет может появиться и после первого импорта
        return package

    yield make
    for module in [m for m in sys.modules if m.startswith(package)]:
        del sys.modules[module]


def events(package: str) -> list[tuple[str, str]]:
    """Журнал on_load/on_unload всех плагинов пакета, по порядку вызова."""
    return sys.modules[package].events
