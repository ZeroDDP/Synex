from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.plugins.manager import PluginManager, PluginStatus
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
            "установлен, не импортируется или нет зависимости; `disabled` установлен, но не "
            "включён в PLUGINS_ENABLED; `registered` ещё не загружен. "
            "Причина в `error`."
        ),
    )
    async def plugins(request: Request) -> list[PluginInfo]:
        manager: PluginManager | None = getattr(request.app.state, "plugins", None)
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

    return router
