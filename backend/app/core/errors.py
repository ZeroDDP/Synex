from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = structlog.get_logger()


class AppError(Exception):
    """Базовая ошибка приложения. Превращается в JSON {"error", "detail"}."""

    code = "app_error"
    status_code = 400

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class WorkerError(AppError):
    """Любая проблема при работе с synex_worker."""

    code = "worker_error"
    status_code = 502


class WorkerConnectionError(WorkerError):
    """Нет IPC-соединения с воркером, обрыв или мусор в канале."""

    code = "worker_unavailable"
    status_code = 503


class WorkerTimeoutError(WorkerError):
    """Воркер не ответил вовремя. Соединение при этом может быть живым."""

    code = "worker_timeout"
    status_code = 504


class WorkerCommandError(WorkerError):
    """Воркер ответил status="error": IPC исправен, упало само действие.

    Например: ошибка SSH-авторизации, "Not connected", "Unknown action".
    """

    code = "worker_command_failed"
    status_code = 502

    def __init__(self, action: str, stderr: str, stdout: str = "", exit_code: int = 1) -> None:
        self.action = action
        self.stderr = stderr
        self.stdout = stdout
        self.exit_code = exit_code
        super().__init__(stderr or f"Worker action '{action}' failed")


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        logger.warning("Request failed", path=request.url.path, error=exc.code, detail=exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.code, "detail": exc.detail},
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error", path=request.url.path, error=repr(exc))
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "detail": "An unexpected error occurred"},
        )
