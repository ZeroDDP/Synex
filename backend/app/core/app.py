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
from app.plugins.manager import PluginManager
from app.worker import worker_manager

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    plugins: PluginManager = app.state.plugins
    logger.info("Starting up", app=settings.APP_NAME, version=__version__)

    await worker_manager.start()
    await plugins.load_all()
    logger.info("Ready", plugins=plugins.names, worker=worker_manager.status())
    try:
        yield
    finally:
        logger.info("Shutting down...")
        await plugins.unload_all()
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
    # UI (будущая папка frontend) ходит в API с другого origin
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_error_handlers(app)

    api_router = build_api_router()
    plugins = PluginManager()
    plugins.discover(api_router)
    app.include_router(api_router)
    app.state.plugins = plugins
    return app
