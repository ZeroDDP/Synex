"""Менеджер плагинов: находит включённые плагины, упорядочивает их по зависимостям,
подключает роутеры и вызывает хуки.

* PLUGINS_ENABLED=* (по умолчанию) включает все установленные плагины: пакеты
  app/plugins/<name>/ с plugin.py. Установленные, но не включённые, видны в
  /api/plugins со статусом disabled.
* Плагин из PLUGINS_ENABLED, который не установлен или не импортируется, пропускается
  (skipped) с понятной причиной; остальные работают.
* Зависимости (BasePlugin.requires) включаются сами и загружаются раньше. Плагин, у
  которого зависимости нет, пропускается.
* Если on_load плагина упал, он и все, кто от него зависит, получают статус failed.
* Плагины включаются и выключаются на ходу, без перезапуска backend (enable, disable;
  REST /api/plugins/{name}/enable и /disable). Включение подгружает и пакет, которого
  не было при старте, вместе с зависимостями, и подключает его роуты. Выключение
  вызывает on_unload, после чего роуты плагина отвечают 404 plugin_disabled (HTTP и
  WebSocket) и пропадают из OpenAPI. Если от плагина зависят включённые, выключение
  отказывает (или выключает и их, cascade).
* Роуты подключаются к приложению один раз и не удаляются: в FastAPI нет публичного
  способа убрать роут, поэтому выключенный плагин закрывает их на входе (зависимость
  роутера), а не снимает с приложения.
* О каждой смене состояния в шину уходит событие plugin.status (хаб пересылает его в
  /ws). События, которые плагин объявил в BasePlugin.events, хаб пересылает в /ws,
  пока плагин включён.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from types import ModuleType
from typing import Any

import structlog
from fastapi import APIRouter, Depends, FastAPI

from app.core.config import settings
from app.core.errors import AppError
from app.core.event_bus import event_bus
from app.core.locks import LoopLock
from app.plugins.base import BasePlugin

logger = structlog.get_logger()

PLUGINS_PACKAGE = "app.plugins"
ALL_PLUGINS = "*"
NOT_INSTALLED = "not installed"
PLUGIN_STATUS_EVENT = "plugin.status"


class PluginStatus(StrEnum):
    REGISTERED = "registered"  # роутер подключён, on_load ещё не вызывался
    LOADED = "loaded"
    FAILED = "failed"  # on_load упал или не загрузилась зависимость
    SKIPPED = "skipped"  # не установлен, не импортируется или нет зависимости
    DISABLED = "disabled"  # установлен, но выключен (не в PLUGINS_ENABLED или на ходу)


@dataclass
class PluginState:
    name: str
    status: PluginStatus
    plugin: BasePlugin | None = None
    error: str | None = None


class PluginUnavailableError(AppError):
    """Плагин включён, но не загрузился: его эндпоинты отвечают 503."""

    code = "plugin_unavailable"
    status_code = 503


class PluginDisabledError(AppError):
    """Запрос к выключенному плагину: его эндпоинты есть, но закрыты."""

    code = "plugin_disabled"
    status_code = 404


class PluginNotFoundError(AppError):
    code = "plugin_not_found"
    status_code = 404


class PluginInUseError(AppError):
    """От плагина зависят включённые плагины: выключать их вместе (cascade=true)."""

    code = "plugin_in_use"
    status_code = 409


class PluginManager:
    """Роутеры подключаются в discover() при создании приложения, до старта сервера,
    поэтому все эндпоинты сразу видны в OpenAPI. Хуки вызывают load_all/unload_all."""

    def __init__(
        self,
        package: str = PLUGINS_PACKAGE,
        forwarder: Any = None,
    ) -> None:
        """forwarder: кто пересылает события шины в /ws (по умолчанию ws_hub)."""
        self.package = package
        self.plugins: dict[str, BasePlugin] = {}  # подключённые, в порядке загрузки
        self._states: dict[str, PluginState] = {}
        self._forwarder = forwarder
        self._app: FastAPI | None = None
        self._prefix = ""
        self._mounted: set[str] = set()  # плагины, чьи роуты подключены к приложению
        self._lock = LoopLock()

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

    def states(self) -> list[PluginState]:
        """Подключённые плагины в порядке загрузки, затем пропущенные и выключенные."""
        registered = [self._states[name] for name in self.plugins]
        skipped = [s for s in self._states.values() if s.status == PluginStatus.SKIPPED]
        disabled = [
            PluginState(name, PluginStatus.DISABLED)
            for name in self.installed()
            if name not in self._states
        ]
        return registered + skipped + disabled

    def installed(self) -> list[str]:
        """Пакеты плагинов: подпапки с plugin.py, по алфавиту."""
        try:
            package = importlib.import_module(self.package)
        except ImportError:
            return []
        return sorted(
            info.name
            for info in pkgutil.iter_modules(getattr(package, "__path__", []))
            if info.ispkg and importlib.util.find_spec(f"{self.package}.{info.name}.plugin")
        )

    # ------------------------------------------------------------ discover

    def discover(self, api_router: APIRouter, enabled: list[str] | None = None) -> None:
        names = enabled if enabled is not None else settings.enabled_plugins
        if ALL_PLUGINS in names:
            index = names.index(ALL_PLUGINS)
            names = [*names[:index], *self.installed(), *names[index + 1 :]]
        queue = list(dict.fromkeys(names))
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
                api_router.include_router(
                    plugin.router,
                    prefix=f"/{name}",
                    tags=[name],
                    dependencies=[Depends(self._availability_check(name))],
                )
                self._mounted.add(name)
            self.plugins[name] = plugin
            self._states[name] = PluginState(name, PluginStatus.REGISTERED, plugin)
            logger.debug("Plugin registered", plugin=name, version=plugin.version)

    def attach(self, app: FastAPI, prefix: str) -> None:
        """Приложение, к которому enable подключает роуты плагинов, не подключённых при
        старте. prefix: префикс API (/api), под ним /<плагин>. OpenAPI приложения
        показывает только включённые плагины."""
        self._app = app
        self._prefix = prefix
        build_schema = app.openapi

        def openapi() -> dict[str, Any]:
            if app.openapi_schema is None:
                schema = build_schema()
                hidden = [self._route_prefix(name) for name in self._disabled_names()]
                paths = schema.get("paths", {})
                schema["paths"] = {
                    path: item
                    for path, item in paths.items()
                    if not any(path == p or path.startswith(p + "/") for p in hidden)
                }
                app.openapi_schema = schema
            return app.openapi_schema

        app.openapi = openapi  # type: ignore[method-assign]

    def _route_prefix(self, name: str) -> str:
        return f"{self._prefix}/{name}"

    def _disabled_names(self) -> list[str]:
        return [n for n, s in self._states.items() if s.status == PluginStatus.DISABLED]

    def _schema_changed(self) -> None:
        if self._app is not None:
            self._app.openapi_schema = None

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

    def _availability_check(self, name: str) -> Callable[[], None]:
        """Зависимость для всех эндпоинтов плагина (HTTP и WebSocket): 503, если он не
        загрузился, и 404, если его выключили."""

        def plugin_is_available() -> None:
            state = self._states.get(name)
            if state is not None and state.status == PluginStatus.FAILED:
                raise PluginUnavailableError(f"Plugin '{name}' is not available: {state.error}")
            if state is not None and state.status == PluginStatus.DISABLED:
                raise PluginDisabledError(
                    f"Plugin '{name}' is disabled. Enable it: POST "
                    f"{self._prefix or '/api'}/plugins/{name}/enable"
                )

        return plugin_is_available

    def _skip(self, name: str, reason: str) -> None:
        logger.warning("Plugin is enabled but skipped", plugin=name, reason=reason)
        self._states[name] = PluginState(name, PluginStatus.SKIPPED, error=reason)

    # ------------------------------------------------------------ lifecycle

    async def load_all(self) -> None:
        async with self._lock:
            for name in list(self.plugins):
                if self._states[name].status != PluginStatus.DISABLED:
                    await self._load(name)

    async def unload_all(self) -> None:
        async with self._lock:
            for name in reversed(list(self.plugins)):
                if self._states[name].status == PluginStatus.LOADED:
                    await self._unload(name, PluginStatus.REGISTERED)

    async def _load(self, name: str) -> bool:
        plugin, state = self.plugins[name], self._states[name]
        if state.status == PluginStatus.LOADED:
            return True
        broken = [d for d in plugin.requires if self.status(d) != PluginStatus.LOADED]
        if broken:
            state.status = PluginStatus.FAILED
            state.error = f"required plugin '{broken[0]}' is not loaded"
            logger.error("Plugin not loaded", plugin=name, reason=state.error)
            await self._announce(state)
            return False
        try:
            await plugin.on_load()
        except Exception as e:
            state.status = PluginStatus.FAILED
            state.error = f"on_load failed: {_describe(e)}"
            logger.exception("Plugin on_load failed", plugin=name, error=repr(e))
            await self._announce(state)
            return False
        state.status = PluginStatus.LOADED
        state.error = None
        for event_type in plugin.events:
            self._forward(event_type)
        logger.info("Plugin loaded", plugin=name, version=plugin.version)
        await self._announce(state)
        return True

    async def _unload(self, name: str, status: PluginStatus) -> None:
        plugin, state = self.plugins[name], self._states[name]
        state.status = status  # новые запросы к плагину сразу получают отказ
        try:
            await plugin.on_unload()
            logger.info("Plugin unloaded", plugin=name)
        except Exception as e:
            logger.exception("Plugin on_unload failed", plugin=name, error=repr(e))
        # После on_unload: то, что плагин сообщил при выгрузке (закрытые сессии,
        # отменённые передачи), ещё доходит до UI
        for event_type in plugin.events:
            self._stop_forward(event_type)
        await self._announce(state)

    # ------------------------------------------------------ enable, disable

    async def enable(self, name: str) -> list[str]:
        """Включить плагин без перезапуска (с зависимостями). Возвращает имена
        включённых сейчас. PluginNotFoundError: пакета нет или он не импортируется;
        PluginUnavailableError: on_load упал."""
        async with self._lock:
            order = self._closure(name)
            enabled = []
            for item in order:
                state = self._states[item]
                if state.status == PluginStatus.LOADED:
                    continue
                self._mount(item)
                state.status = PluginStatus.REGISTERED
                self._schema_changed()
                if not await self._load(item):
                    raise PluginUnavailableError(f"Plugin '{item}' did not start: {state.error}")
                enabled.append(item)
            return enabled

    async def disable(self, name: str, *, cascade: bool = False) -> list[str]:
        """Выключить плагин без перезапуска: on_unload и роуты снимаются с приложения.
        Если от него зависят включённые плагины, без cascade отказ (PluginInUseError),
        с cascade они выключаются раньше. Возвращает имена выключенных."""
        async with self._lock:
            state = self._states.get(name)
            if state is None or state.plugin is None:
                if name in self.installed():
                    return []  # уже выключен
                raise PluginNotFoundError(f"Plugin '{name}' is not installed")
            dependents = self._dependents(name)
            if dependents and not cascade:
                raise PluginInUseError(
                    f"Plugins {', '.join(dependents)} depend on '{name}': disable them first "
                    "or pass cascade=true"
                )
            disabled = []
            for item in [*dependents, name]:
                item_state = self._states[item]
                if item_state.status == PluginStatus.DISABLED:
                    continue
                item_state.error = None
                if item_state.status == PluginStatus.LOADED:
                    await self._unload(item, PluginStatus.DISABLED)
                else:
                    item_state.status = PluginStatus.DISABLED
                    await self._announce(item_state)
                self._schema_changed()
                disabled.append(item)
            return disabled

    def _closure(self, name: str) -> list[str]:
        """name и все его зависимости, зависимости раньше. Подгружает пакеты."""
        order: list[str] = []
        visiting: set[str] = set()

        def visit(item: str) -> None:
            if item in order:
                return
            if item in visiting:
                raise PluginUnavailableError(f"Dependency cycle with plugin '{item}'")
            visiting.add(item)
            plugin = self.plugins.get(item)
            if plugin is None:
                self._states.pop(item, None)  # прошлый skipped: попробуем ещё раз
                importlib.invalidate_caches()  # пакет могли установить после старта
                plugin = self._import(item)
                if plugin is None:
                    reason = self._states[item].error
                    self._states.pop(item, None)
                    raise PluginNotFoundError(f"Plugin '{item}' cannot be loaded: {reason}")
                self.plugins[item] = plugin
                self._states[item] = PluginState(item, PluginStatus.DISABLED, plugin)
            for dependency in plugin.requires:
                visit(dependency)
            visiting.discard(item)
            order.append(item)

        visit(name)
        return order

    def _dependents(self, name: str) -> list[str]:
        """Включённые плагины, которые зависят от name (прямо или через других), в
        порядке выключения: сначала самые зависимые."""
        result: list[str] = []

        def visit(item: str) -> None:
            for other, plugin in self.plugins.items():
                if item in plugin.requires and other not in result:
                    if self._states[other].status != PluginStatus.DISABLED:
                        visit(other)
                        if other not in result:
                            result.append(other)

        visit(name)
        return result

    def _mount(self, name: str) -> None:
        plugin = self.plugins[name]
        if name in self._mounted or plugin.router is None:
            return
        if self._app is None:
            raise RuntimeError("PluginManager.attach(app) was not called")
        self._app.include_router(
            plugin.router,
            prefix=self._route_prefix(name),
            tags=[name],
            dependencies=[Depends(self._availability_check(name))],
        )
        self._mounted.add(name)
        logger.info("Plugin routes mounted", plugin=name)

    # ------------------------------------------------------------- events

    async def _announce(self, state: PluginState) -> None:
        plugin = state.plugin
        await event_bus.publish(
            PLUGIN_STATUS_EVENT,
            {
                "name": state.name,
                "status": str(state.status),
                "version": plugin.version if plugin else None,
                "error": state.error,
            },
        )

    def _hub(self) -> Any:
        if self._forwarder is None:
            from app.ws import ws_hub  # здесь, а не наверху: app.ws импортирует плагины

            self._forwarder = ws_hub
        return self._forwarder

    def _forward(self, event_type: str) -> None:
        self._hub().forward(event_type)

    def _stop_forward(self, event_type: str) -> None:
        self._hub().stop_forward(event_type)

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
