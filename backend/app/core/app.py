from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.router import build_api_router
from app.core.config import settings
from app.core.errors import register_error_handlers
from app.core.logging import setup_logging
from app.core.security import (
    LocalAccessMiddleware,
    allowed_hosts,
    allowed_origins,
    warn_if_exposed,
)
from app.plugins.manager import PluginManager
from app.worker import worker_manager
from app.ws import METRICS_EVENT, FakeMetricsPublisher, ws_hub
from app.ws import router as ws_router

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    plugins: PluginManager = app.state.plugins
    logger.info("Starting up", app=settings.APP_NAME, version=__version__)
    warn_if_exposed(settings)

    await worker_manager.start()
    # До плагинов: событие, опубликованное в on_load, уже дойдёт до UI
    ws_hub.forward(METRICS_EVENT)
    await plugins.load_all()

    fake_metrics: FakeMetricsPublisher | None = None
    if settings.WS_FAKE_METRICS:
        fake_metrics = FakeMetricsPublisher(settings.WS_FAKE_METRICS_INTERVAL)
        fake_metrics.start()

    logger.info(
        "Ready",
        plugins=plugins.names,
        worker=worker_manager.status(),
        ws_streams=ws_hub.streams,
        fake_metrics=fake_metrics is not None,
    )
    try:
        yield
    finally:
        logger.info("Shutting down...")
        if fake_metrics is not None:
            await fake_metrics.stop()
        await plugins.unload_all()
        ws_hub.stop_forwarding()
        await worker_manager.stop()
        logger.info("Shutdown complete")


def create_app() -> FastAPI:
    setup_logging(debug=settings.DEBUG)

    app = FastAPI(
        title=settings.APP_NAME,
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs" if settings.DEBUG else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.DEBUG else None,
    )
    # UI (будущая папка frontend) может ходить в API с другого origin: только из
    # ALLOWED_ORIGINS. Чужие Origin и Host отсекает LocalAccessMiddleware (он снаружи).
    origins = allowed_origins(settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(LocalAccessMiddleware, origins=origins, hosts=allowed_hosts(settings))
    register_error_handlers(app)

    api_router = build_api_router()
    plugins = PluginManager()
    plugins.discover(api_router)
    app.include_router(api_router)
    app.include_router(ws_router)
    app.state.plugins = plugins
    return app
