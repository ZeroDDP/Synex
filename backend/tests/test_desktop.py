"""Отдача собранного UI backend (UI_DIR)."""

from __future__ import annotations

from pathlib import Path

import httpx

from app.core.app import create_app
from app.core.config import settings


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
