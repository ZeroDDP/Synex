from __future__ import annotations

from fastapi import APIRouter


class BasePlugin:
    """Плагин backend: свой роутер под /api/<name> и хуки жизненного цикла.

    Пакет плагина: app/plugins/<name>/plugin.py с подклассом BasePlugin.
    Включается через PLUGINS_ENABLED.
    """

    name: str = "unnamed"
    version: str = "0.0.0"
    router: APIRouter | None = None

    async def on_load(self) -> None:
        """Вызывается при старте приложения, после запуска воркера."""

    async def on_unload(self) -> None:
        """Вызывается при остановке приложения, до остановки воркера."""
