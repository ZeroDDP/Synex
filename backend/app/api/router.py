from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request
from pydantic import BaseModel

from app.external.actions import router as action_router
from app.external.routes import router as external_router
from app.plugins.manager import PluginManager, PluginNotFoundError, PluginStatus
from app.worker import worker_manager

API_PREFIX = "/api"


class HealthResponse(BaseModel):
    status: str
    plugins: list[str]
    worker: dict[str, Any]


class PluginInfo(BaseModel):
    name: str
    version: str | None
    description: str
    requires: list[str]
    status: PluginStatus
    error: str | None


def build_api_router() -> APIRouter:
    """Корневой роутер /api. Плагины добавляют сюда свои роутеры (/api/<plugin>)."""
    router = APIRouter(prefix=API_PREFIX)
    router.include_router(external_router)
    router.include_router(action_router)

    @router.get("/health", response_model=HealthResponse, tags=["system"])
    async def health(request: Request) -> HealthResponse:
        plugins = getattr(request.app.state, "plugins", None)
        return HealthResponse(
            status="ok",
            plugins=plugins.loaded if plugins is not None else [],
            worker=worker_manager.status(),
        )

    @router.get(
        "/plugins",
        response_model=list[PluginInfo],
        tags=["system"],
        summary="Плагины и их состояние",
        description=(
            "status: `loaded` работает; `failed` включён, но on_load упал или не загрузилась "
            "зависимость (его эндпоинты отвечают 503 `plugin_unavailable`); `skipped` не "
            "установлен, не импортируется или нет зависимости; `disabled` установлен, но "
            "выключен (не в PLUGINS_ENABLED или через /disable, в том числе в прошлый "
            "запуск), его эндпоинты отвечают 404 "
            "`plugin_disabled`; `registered` ещё не загружен. Причина в `error`. Смена "
            "состояния приходит в /ws событием `plugin.status`."
        ),
    )
    async def plugins(request: Request) -> list[PluginInfo]:
        manager: PluginManager | None = getattr(request.app.state, "plugins", None)
        return _plugin_list(manager)

    @router.post(
        "/plugins/{name}/enable",
        response_model=PluginChange,
        tags=["system"],
        summary="Включить плагин без перезапуска (вместе с зависимостями)",
        description=(
            "Подгружает и пакет, которого не было при старте. Роуты плагина появляются в "
            "API, его события начинают приходить в `/ws`. 404 `plugin_not_found`: пакета "
            "нет или он не импортируется; 503 `plugin_unavailable`: плагин не запустился "
            "(причина в `detail`). Выбор запоминается: после перезапуска backend плагин "
            "снова включён (`PLUGINS_REMEMBER`)."
        ),
    )
    async def enable_plugin(name: PluginName, request: Request) -> PluginChange:
        manager = _manager(request)
        changed = await manager.enable(name)
        return PluginChange(changed=changed, plugins=_plugin_list(manager))

    @router.post(
        "/plugins/{name}/disable",
        response_model=PluginChange,
        tags=["system"],
        summary="Выключить плагин без перезапуска",
        description=(
            "on_unload плагина; дальше его эндпоинты отвечают 404 `plugin_disabled` и "
            "пропадают из OpenAPI. Если от плагина зависят включённые, 409 "
            "`plugin_in_use`; с `cascade=true` они выключаются тоже. Выключение ssh "
            "закрывает все SSH-сессии. Выбор запоминается: после перезапуска backend "
            "плагин остаётся выключенным (`PLUGINS_REMEMBER`)."
        ),
    )
    async def disable_plugin(
        name: PluginName,
        request: Request,
        cascade: Annotated[
            bool, Query(description="Выключить и зависящие от него плагины")
        ] = False,
    ) -> PluginChange:
        manager = _manager(request)
        changed = await manager.disable(name, cascade=cascade)
        return PluginChange(changed=changed, plugins=_plugin_list(manager))

    return router


PluginName = Annotated[str, Path(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class PluginChange(BaseModel):
    changed: list[str]
    plugins: list[PluginInfo]


def _manager(request: Request) -> PluginManager:
    manager: PluginManager | None = getattr(request.app.state, "plugins", None)
    if manager is None:
        raise PluginNotFoundError("Plugins are not available in this application")
    return manager


def _plugin_list(manager: PluginManager | None) -> list[PluginInfo]:
    if manager is None:
        return []
    return [
        PluginInfo(
            name=state.name,
            version=state.plugin.version if state.plugin else None,
            description=state.plugin.description if state.plugin else "",
            requires=list(state.plugin.requires) if state.plugin else [],
            status=state.status,
            error=state.error,
        )
        for state in manager.states()
    ]
