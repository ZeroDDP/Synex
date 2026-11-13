from __future__ import annotations

from fastapi import APIRouter


class BasePlugin:
    """Плагин backend: свой роутер под /api/<name> и хуки жизненного цикла.

    Пакет плагина: app/plugins/<name>/plugin.py с подклассом BasePlugin.
    Включается через PLUGINS_ENABLED.

    requires: плагины, без которых этот не работает (например, docker выполняет
    команды через сессии плагина ssh). Они загружаются раньше и включаются сами, даже
    если их нет в PLUGINS_ENABLED. Если зависимость не загрузилась, этот плагин тоже
    не загружается.

    events: события шины, которые плагин публикует для UI: пока плагин включён, хаб
    пересылает их в /ws.

    Плагин можно выключить и включить на ходу (PluginManager.enable, disable): on_load
    и on_unload должны выдерживать повторный вызов.
    """

    name: str = "unnamed"
    version: str = "0.0.0"
    description: str = ""
    requires: tuple[str, ...] = ()
    events: tuple[str, ...] = ()
    router: APIRouter | None = None

    async def on_load(self) -> None:
        """Вызывается при старте приложения (после запуска воркера) и при включении."""

    async def on_unload(self) -> None:
        """Вызывается при выключении плагина и при остановке приложения."""
