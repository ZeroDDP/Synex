from __future__ import annotations

import importlib
import importlib.util
from types import ModuleType

import structlog
from fastapi import APIRouter

from app.core.config import settings
from app.plugins.base import BasePlugin

logger = structlog.get_logger()

PLUGINS_PACKAGE = "app.plugins"


class PluginManager:
    """Находит включённые плагины, подключает их роутеры и вызывает хуки.

    Роутеры подключаются в discover() при создании приложения, до старта сервера,
    поэтому все эндпоинты сразу видны в OpenAPI.
    """

    def __init__(self) -> None:
        self.plugins: dict[str, BasePlugin] = {}

    @property
    def names(self) -> list[str]:
        return list(self.plugins)

    def discover(self, api_router: APIRouter, enabled: list[str] | None = None) -> None:
        for name in enabled if enabled is not None else settings.enabled_plugins:
            if name in self.plugins:
                continue
            if importlib.util.find_spec(f"{PLUGINS_PACKAGE}.{name}") is None:
                logger.warning("Plugin is enabled but not installed", plugin=name)
                continue
            try:
                module = importlib.import_module(f"{PLUGINS_PACKAGE}.{name}.plugin")
                plugin_cls = self._find_plugin_class(module)
                if plugin_cls is None:
                    logger.error("No BasePlugin subclass in plugin module", plugin=name)
                    continue
                plugin = plugin_cls()
            except Exception as e:
                logger.exception("Failed to import plugin", plugin=name, error=repr(e))
                continue

            if plugin.router is not None:
                api_router.include_router(plugin.router, prefix=f"/{name}", tags=[name])
            self.plugins[name] = plugin
            logger.debug("Plugin registered", plugin=name, version=plugin.version)

    async def load_all(self) -> None:
        for name, plugin in self.plugins.items():
            try:
                await plugin.on_load()
                logger.info("Plugin loaded", plugin=name, version=plugin.version)
            except Exception as e:
                logger.exception("Plugin on_load failed", plugin=name, error=repr(e))

    async def unload_all(self) -> None:
        for name, plugin in reversed(list(self.plugins.items())):
            try:
                await plugin.on_unload()
                logger.info("Plugin unloaded", plugin=name)
            except Exception as e:
                logger.exception("Plugin on_unload failed", plugin=name, error=repr(e))

    @staticmethod
    def _find_plugin_class(module: ModuleType) -> type[BasePlugin] | None:
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and issubclass(value, BasePlugin)
                and value is not BasePlugin
                and value.__module__ == module.__name__
            ):
                return value
        return None
