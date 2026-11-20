"""Запуск как приложение (app/desktop.py) и отдача собранного UI backend (UI_DIR)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from app import desktop
from app.core.app import create_app
from app.core.config import settings


@pytest.fixture
def clean_env(monkeypatch, tmp_path: Path) -> Iterator[Path]:
    """Папка данных теста и окружение без настроек, которые задаёт desktop. После теста
    окружение возвращается целиком: prepare_environment пишет прямо в os.environ."""
    saved = dict(os.environ)
    for name in ("PORT", "HOST", "UI_DIR", "UI_DEV_SERVER", "SYNEX_VERSION", "DEBUG",
                 "RUST_EXECUTABLE_PATH_WIN32", "RUST_EXECUTABLE_PATH_UNIX"):  # fmt: skip
        os.environ.pop(name, None)
    data = tmp_path / "data"
    monkeypatch.setattr(desktop, "_user_data_dir_early", lambda: data)
    try:
        yield data
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_prepare_environment_reads_user_settings(clean_env: Path, monkeypatch, tmp_path: Path):
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text('<div id="app"></div>')
    worker = tmp_path / "synex_worker"
    worker.write_text("")
    where = desktop.Layout(frozen=True, root=tmp_path, ui=ui, worker=worker, version="0.1.0")

    env_file = desktop.prepare_environment(where, 8123)
    assert env_file == clean_env / "synex.env"
    assert "DEBUG=true" in env_file.read_text(encoding="utf-8")  # шаблон с подсказками
    assert os.environ["PORT"] == "8123" and os.environ["HOST"] == "127.0.0.1"
    assert os.environ["UI_DIR"] == str(ui)
    assert os.environ["RUST_EXECUTABLE_PATH_UNIX"] == str(worker)
    assert os.environ["UI_DEV_SERVER"] == "false"
    assert os.environ["SYNEX_VERSION"] == "0.1.0"

    # Настройки пользователя из synex.env; переменные окружения важнее файла
    for name in ("PORT", "DEBUG"):
        monkeypatch.delenv(name, raising=False)
    env_file.write_text("PORT=9001\nDEBUG=true\n", encoding="utf-8")
    monkeypatch.setenv("DEBUG", "false")
    desktop.prepare_environment(where, 8123)
    assert os.environ["PORT"] == "9001" and os.environ["DEBUG"] == "false"


def test_layout_from_sources():
    where = desktop.layout()
    assert where.frozen is False
    assert where.root.name == "backend"
    assert where.worker is None  # из исходников воркер ищется как обычно


def test_nothing_runs_on_a_free_port():
    assert desktop.running_synex("http://127.0.0.1:1") is False


def test_version_flag(capsys, monkeypatch):
    monkeypatch.setattr(
        desktop, "layout", lambda: desktop.Layout(False, Path("."), None, None, "0.1.0")
    )
    assert desktop.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "Synex 0.1.0"


async def test_backend_serves_the_built_ui(monkeypatch, tmp_path: Path):
    ui = tmp_path / "dist"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text('<div id="app"></div>')
    (ui / "assets" / "app.js").write_text("console.log(1)")
    monkeypatch.setattr(settings, "UI_DIR", ui)
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        page = await client.get("/")
        assert page.status_code == 200 and 'id="app"' in page.text
        assert (await client.get("/assets/app.js")).text == "console.log(1)"
        # API важнее UI: неизвестный путь под /api — обычный JSON 404
        missing = await client.get("/api/nothing")
        assert missing.status_code == 404 and missing.json() == {"detail": "Not Found"}
        assert (await client.get("/api/health")).status_code == 200


async def test_no_ui_without_ui_dir(monkeypatch):
    monkeypatch.setattr(settings, "UI_DIR", None)
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        assert (await client.get("/")).status_code == 404
