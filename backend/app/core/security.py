"""Доступ к API только с этой машины и только из разрешённых приложений.

У API нет авторизации: он слушает 127.0.0.1 и доверяет локальным клиентам. Но браузер
тоже локальный клиент. Без проверок любая открытая в нём страница могла бы прочитать
список SSH-сессий (fetch) и выполнять команды на серверах через WebSocket терминала:
WebSocket не подчиняется CORS. Поэтому:

* Origin. Браузер всегда передаёт его в кросс-доменных запросах и в рукопожатии
  WebSocket. Разрешены сам backend (http://127.0.0.1:PORT, http://localhost:PORT) и
  ALLOWED_ORIGINS. Запросы без Origin (curl, PowerShell, настольное приложение)
  проходят. Иначе 403, WebSocket закрывается до accept (клиент получит 403).
* Host. Защита от DNS rebinding: чужой домен, который резолвится в 127.0.0.1, не
  пройдёт, потому что в Host будет его имя. Разрешены 127.0.0.1, localhost, [::1],
  HOST из настроек и ALLOWED_HOSTS.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

import structlog

from app.core.config import Settings

logger = structlog.get_logger()

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
WS_POLICY_VIOLATION = 1008


def allowed_origins(settings: Settings) -> list[str]:
    """Сам backend плюс ALLOWED_ORIGINS. Порядок сохраняется (для CORS)."""
    own = [f"http://{host}:{settings.PORT}" for host in ("127.0.0.1", "localhost", "[::1]")]
    extra = [o.strip().rstrip("/") for o in settings.ALLOWED_ORIGINS.split(",") if o.strip()]
    return list(dict.fromkeys([*own, *extra]))


def allowed_hosts(settings: Settings) -> set[str] | None:
    """Имена в заголовке Host. None: проверка выключена (HOST не loopback и
    ALLOWED_HOSTS не задан: заранее неизвестно, по какому имени придут клиенты)."""
    extra = {h.strip().lower() for h in settings.ALLOWED_HOSTS.split(",") if h.strip()}
    if settings.HOST not in LOOPBACK_HOSTS and not extra:
        return None
    return {*LOOPBACK_HOSTS, settings.HOST.lower(), *extra}


def host_name(value: str) -> str:
    """Имя из заголовка Host без порта: "127.0.0.1:8000" → "127.0.0.1", "[::1]:80" → "::1"."""
    value = value.strip().lower()
    if value.startswith("["):
        return value[1 : value.find("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


class LocalAccessMiddleware:
    """ASGI-middleware: проверяет Origin и Host у HTTP и WebSocket."""

    def __init__(self, app: Any, origins: Iterable[str], hosts: set[str] | None) -> None:
        self.app = app
        self.origins = {o.lower() for o in origins}
        self.hosts = hosts

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        host = headers.get(b"host", b"").decode("latin-1")
        origin = headers.get(b"origin", b"").decode("latin-1").rstrip("/")

        problem: tuple[str, str] | None = None
        if self.hosts is not None and host and host_name(host) not in self.hosts:
            problem = ("forbidden_host", f"Host '{host}' is not allowed")
        elif origin and origin.lower() not in self.origins:
            problem = (
                "forbidden_origin",
                f"Origin '{origin}' is not allowed. Add it to ALLOWED_ORIGINS to let this "
                "web page use the API",
            )

        if problem is None:
            await self.app(scope, receive, send)
            return

        code, detail = problem
        logger.warning(
            "Request rejected", error=code, path=scope.get("path"), host=host, origin=origin
        )
        if scope["type"] == "websocket":
            # Закрытие до accept: клиент получает отказ рукопожатия (HTTP 403)
            await send({"type": "websocket.close", "code": WS_POLICY_VIOLATION})
            return
        body = json.dumps({"error": code, "detail": detail}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def warn_if_exposed(settings: Settings) -> None:
    if settings.HOST not in LOOPBACK_HOSTS:
        logger.warning(
            "API is listening on a non-loopback address and has no authentication: anyone "
            "who can reach this port controls your SSH sessions. Use HOST=127.0.0.1",
            host=settings.HOST,
        )
