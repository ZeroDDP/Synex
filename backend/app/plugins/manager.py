"""Менеджер плагинов: находит включённые плагины, упорядочивает их по зависимостям,
подключает роутеры и вызывает хуки.

* Плагин из PLUGINS_ENABLED, который не установлен или не импортируется, пропускается
  (skipped) с понятной причиной; остальные работают.
* Зависимости (BasePlugin.requires) включаются сами и загружаются раньше. Плагин, у
  которого зависимости нет, пропускается.
* Если on_load плагина упал, он и все, кто от него зависит, получают статус failed.
"""

from __future__ import annotations

import importlib
import importlib.util
from dataclasses import dataclass
from enum import StrEnum
from types import ModuleType

import structlog
from fastapi import APIRouter

from app.core.config import settings
from app.plugins.base import BasePlugin

logger = structlog.get_logger()

PLUGINS_PACKAGE = "app.plugins"
NOT_INSTALLED = "not installed"


class PluginStatus(StrEnum):
    REGISTERED = "registered"  # роутер подключён, on_load ещё не вызывался
    LOADED = "loaded"
    FAILED = "failed"  # on_load упал или не загрузилась зависимость
    SKIPPED = "skipped"  # не установлен, не импортируется или нет зависимости


@dataclass
class PluginState:
    name: str
    status: PluginStatus
    plugin: BasePlugin | None = None
    error: str | None = None


class PluginManager:
    """Роутеры подключаются в discover() при создании приложения, до старта сервера,
    поэтому все эндпоинты сразу видны в OpenAPI. Хуки вызывают load_all/unload_all."""

    def __init__(self, package: str = PLUGINS_PACKAGE) -> None:
        self.package = package
        self.plugins: dict[str, BasePlugin] = {}  # подключённые, в порядке загрузки
        self._states: dict[str, PluginState] = {}

    @property
    def names(self) -> list[str]:
        return list(self.plugins)

    @property
    def loaded(self) -> list[str]:
        return [name for name in self.plugins if self.status(name) == PluginStatus.LOADED]

    def status(self, name: str) -> PluginStatus | None:
        state = self._states.get(name)
        return state.status if state else None

    def state(self, name: str) -> PluginState | None:
        return self._states.get(name)

    # ------------------------------------------------------------ discover

    def discover(self, api_router: APIRouter, enabled: list[str] | None = None) -> None:
        queue = list(dict.fromkeys(enabled if enabled is not None else settings.enabled_plugins))
        found: dict[str, BasePlugin] = {}
        index = 0
        while index < len(queue):
            name = queue[index]
            index += 1
            if name in self._states:
                continue
            plugin = self._import(name)
            if plugin is None:
                continue
            found[name] = plugin
            for dependency in plugin.requires:
                if dependency not in queue:
                    logger.info("Plugin enabled as a dependency", plugin=dependency, of=name)
                    queue.append(dependency)

        for name in self._order(found):
            plugin = found[name]
            if plugin.router is not None:
                api_router.include_router(plugin.router, prefix=f"/{name}", tags=[name])
            self.plugins[name] = plugin
            self._states[name] = PluginState(name, PluginStatus.REGISTERED, plugin)
            logger.debug("Plugin registered", plugin=name, version=plugin.version)

    def _import(self, name: str) -> BasePlugin | None:
        try:
            installed = importlib.util.find_spec(f"{self.package}.{name}") is not None
        except (ImportError, ValueError):
            installed = False
        if not installed:
            return self._skip(name, NOT_INSTALLED)
        try:
            module = importlib.import_module(f"{self.package}.{name}.plugin")
            plugin_cls = self._find_plugin_class(module)
            if plugin_cls is None:
                return self._skip(name, "no BasePlugin subclass in plugin.py")
            return plugin_cls()
        except Exception as e:
            logger.exception("Failed to import plugin", plugin=name, error=repr(e))
            return self._skip(name, f"import failed: {_describe(e)}")

    def _order(self, found: dict[str, BasePlugin]) -> list[str]:
        """Зависимости раньше зависимых, остальное в порядке PLUGINS_ENABLED."""
        pending = dict(found)
        changed = True
        while changed:  # без зависимости пропускаются и те, кто зависит от пропущенных
            changed = False
            for name, plugin in list(pending.items()):
                missing = [d for d in plugin.requires if d not in pending and d not in self.plugins]
                if missing:
                    dependency = self._states.get(missing[0])
                    absent = dependency is not None and dependency.error == NOT_INSTALLED
                    why = NOT_INSTALLED if absent else "skipped"
                    self._skip(name, f"requires plugin '{missing[0]}', which is {why}")
                    del pending[name]
                    changed = True

        order: list[str] = []
        while pending:
            ready = next(
                (
                    name
                    for name, plugin in pending.items()
                    if all(d in order or d in self.plugins for d in plugin.requires)
                ),
                None,
            )
            if ready is None:
                for name in pending:
                    self._skip(name, f"dependency cycle: {', '.join(pending)}")
                break
            order.append(ready)
            del pending[ready]
        return order

    def _skip(self, name: str, reason: str) -> None:
        logger.warning("Plugin is enabled but skipped", plugin=name, reason=reason)
        self._states[name] = PluginState(name, PluginStatus.SKIPPED, error=reason)

    # ------------------------------------------------------------ lifecycle

    async def load_all(self) -> None:
        for name, plugin in self.plugins.items():
            state = self._states[name]
            if state.status == PluginStatus.LOADED:
                continue
            broken = [d for d in plugin.requires if self.status(d) != PluginStatus.LOADED]
            if broken:
                state.status = PluginStatus.FAILED
                state.error = f"required plugin '{broken[0]}' is not loaded"
                logger.error("Plugin not loaded", plugin=name, reason=state.error)
                continue
            try:
                await plugin.on_load()
            except Exception as e:
                state.status = PluginStatus.FAILED
                state.error = f"on_load failed: {_describe(e)}"
                logger.exception("Plugin on_load failed", plugin=name, error=repr(e))
                continue
            state.status = PluginStatus.LOADED
            state.error = None
            logger.info("Plugin loaded", plugin=name, version=plugin.version)

    async def unload_all(self) -> None:
        for name, plugin in reversed(list(self.plugins.items())):
            state = self._states[name]
            if state.status != PluginStatus.LOADED:
                continue
            try:
                await plugin.on_unload()
                logger.info("Plugin unloaded", plugin=name)
            except Exception as e:
                logger.exception("Plugin on_unload failed", plugin=name, error=repr(e))
            state.status = PluginStatus.REGISTERED

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


def _describe(error: Exception) -> str:
    text = str(error)
    return f"{type(error).__name__}: {text}" if text else type(error).__name__
