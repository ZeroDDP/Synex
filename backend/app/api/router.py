from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.worker import worker_manager

API_PREFIX = "/api"


class HealthResponse(BaseModel):
    status: str
    plugins: list[str]
    worker: dict[str, Any]


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

    return router
