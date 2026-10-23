"""Точка входа: `uv run python -m app.main` из папки backend."""

import uvicorn

from app.core.config import settings


def main() -> None:
    # Без reload: на Windows uvicorn с --reload/--workers переключается на
    # SelectorEventLoop, а Named Pipe к воркеру работает только на ProactorEventLoop.
    uvicorn.run(
        "app.core.app:create_app",
        factory=True,
        host=settings.HOST,
        port=settings.PORT,
        log_level="debug" if settings.DEBUG else "info",
    )


if __name__ == "__main__":
    main()
