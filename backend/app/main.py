"""Точка входа: `uv run python -m app.main` из папки backend."""

from __future__ import annotations

import re
import sys

from pydantic import ValidationError

# Значения этих настроек не печатаем даже в сообщении об ошибке
_SECRET_NAME = re.compile(r"(KEY|PASSWORD|SECRET|TOKEN)$", re.IGNORECASE)


def describe_settings_error(error: ValidationError) -> str:
    """Понятное сообщение вместо трейсбэка pydantic: какая настройка и что не так."""
    lines = ["Invalid settings in backend/.env or environment variables:"]
    for item in error.errors():
        name = ".".join(str(part) for part in item["loc"]) or "?"
        value = "***" if _SECRET_NAME.search(name) else repr(item.get("input"))
        lines.append(f"  {name}={value}: {item['msg']}")
    lines.append("Fix the values or remove them to use defaults (see backend/.env.example)")
    return "\n".join(lines)


def main() -> None:
    try:
        from app.core.config import settings
    except ValidationError as error:
        print(describe_settings_error(error), file=sys.stderr)
        raise SystemExit(1) from None
    except UnicodeDecodeError:
        print("backend/.env must be saved in UTF-8", file=sys.stderr)
        raise SystemExit(1) from None

    import uvicorn

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
