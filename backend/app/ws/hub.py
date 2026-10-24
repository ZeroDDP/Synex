"""Хаб WebSocket-соединений /ws: доставка данных из backend в UI.

Протокол: JSON в текстовых фреймах, каждое сообщение в конверте
{"type": str, "ts": int (Unix, мс), "data": ...}.

* Сервер → UI: "hello" сразу после подключения, затем потоки из шины событий
  (сейчас "metrics" каждые 500 мс), "pong" в ответ на "ping", "error", если
  сообщение клиента не разобрано.
* UI → сервер: {"type": "ping", "data": <что угодно>}; data вернётся в "pong".

Источники данных ничего не знают о WebSocket: они публикуют события в event_bus,
а хаб пересылает выбранные события (forward) всем клиентам.

У каждого клиента своя очередь и своя задача-отправитель, поэтому медленный клиент
не задерживает остальных и источник данных. Если очередь переполнена, выбрасывается
самое старое сообщение: для потоковых данных важна свежесть, а не полнота.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from starlette.websockets import WebSocketState

from app import __version__
from app.core.config import settings
from app.core.event_bus import event_bus

logger = structlog.get_logger()

PROTOCOL_VERSION = 1
QUEUE_SIZE = 64

# Коды ошибок в {"type": "error", "data": {"error": код, "detail": текст}}
ERROR_BAD_MESSAGE = "bad_message"
ERROR_UNKNOWN_TYPE = "unknown_type"

# Коды закрытия WebSocket (RFC 6455)
CLOSE_GOING_AWAY = 1001
CLOSE_INTERNAL_ERROR = 1011


def encode(message_type: str, data: Any = None) -> str:
    """Конверт сообщения в JSON. Pydantic-модели превращаются в обычный JSON."""
    if isinstance(data, BaseModel):
        data = data.model_dump(mode="json")
    message = {"type": message_type, "ts": time.time_ns() // 1_000_000, "data": data}
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


class WebSocketClient:
    """Одно соединение /ws и его очередь исходящих сообщений."""

    def __init__(self, websocket: WebSocket, queue_size: int = QUEUE_SIZE) -> None:
        self.websocket = websocket
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_size)
        self.dropped = 0

    def push(self, text: str) -> None:
        """Поставить сообщение в очередь, не дожидаясь отправки."""
        if self.queue.full():
            self.queue.get_nowait()
            self.dropped += 1
        self.queue.put_nowait(text)


class WebSocketHub:
    """Все подключённые клиенты /ws и пересылка им событий шины."""

    def __init__(self) -> None:
        self._clients: set[WebSocketClient] = set()
        self._forwarded: dict[str, Callable[[Any], Awaitable[None]]] = {}

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def streams(self) -> list[str]:
        """Типы сообщений, которые хаб пересылает из шины событий."""
        return list(self._forwarded)

    def register(self, websocket: WebSocket) -> WebSocketClient:
        client = WebSocketClient(websocket)
        self._clients.add(client)
        return client

    def unregister(self, client: WebSocketClient) -> None:
        self._clients.discard(client)

    def broadcast(self, message_type: str, data: Any = None) -> None:
        """Отправить сообщение всем клиентам. Не блокирует: только кладёт в очереди."""
        if not self._clients:
            return
        text = encode(message_type, data)
        for client in list(self._clients):
            client.push(text)

    def forward(self, event_type: str) -> None:
        """Пересылать клиентам события шины event_type как сообщения того же типа."""
        if event_type in self._forwarded:
            return

        # Только async: синхронные обработчики EventBus выполняет в пуле потоков,
        # а asyncio.Queue не потокобезопасна.
        async def handler(data: Any) -> None:
            self.broadcast(event_type, data)

        self._forwarded[event_type] = handler
        event_bus.subscribe(event_type, handler)

    def stop_forwarding(self) -> None:
        for event_type, handler in self._forwarded.items():
            event_bus.unsubscribe(event_type, handler)
        self._forwarded.clear()

    # ------------------------------------------------------------ connection

    async def serve(self, websocket: WebSocket) -> None:
        """Обслуживает одно соединение до отключения клиента или остановки сервера."""
        await websocket.accept()
        client = self.register(websocket)
        peer = _peer(websocket)
        logger.info("WebSocket client connected", peer=peer, clients=self.client_count)
        client.push(encode("hello", self._hello()))

        sender = asyncio.create_task(self._send_loop(client), name=f"ws-send {peer}")
        receiver = asyncio.create_task(self._receive_loop(client), name=f"ws-receive {peer}")
        try:
            await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
        finally:
            self.unregister(client)
            sender.cancel()
            receiver.cancel()
            results = await asyncio.gather(sender, receiver, return_exceptions=True)
            errors = [
                r
                for r in results
                if isinstance(r, Exception) and not isinstance(r, WebSocketDisconnect)
            ]
            for error in errors:
                logger.warning("WebSocket connection failed", peer=peer, error=repr(error))
            await _close(websocket, CLOSE_INTERNAL_ERROR if errors else CLOSE_GOING_AWAY)
            logger.info(
                "WebSocket client disconnected",
                peer=peer,
                code=results[1] if isinstance(results[1], int) else None,
                dropped=client.dropped,
                clients=self.client_count,
            )

    def _hello(self) -> dict[str, Any]:
        return {
            "app": settings.APP_NAME,
            "version": __version__,
            "protocol": PROTOCOL_VERSION,
            "streams": self.streams,
        }

    @staticmethod
    async def _send_loop(client: WebSocketClient) -> None:
        while True:
            text = await client.queue.get()
            await client.websocket.send_text(text)

    async def _receive_loop(self, client: WebSocketClient) -> int:
        """Читает сообщения клиента. Возвращает код закрытия, когда клиент отключился."""
        while True:
            message = await client.websocket.receive()
            if message["type"] == "websocket.disconnect":
                return int(message.get("code") or 1000)
            self._handle(client, message.get("text"))

    def _handle(self, client: WebSocketClient, text: str | None) -> None:
        if text is None:
            client.push(_error(ERROR_BAD_MESSAGE, "Expected a JSON text frame"))
            return
        try:
            message = json.loads(text)
        except ValueError:
            client.push(_error(ERROR_BAD_MESSAGE, "Message is not valid JSON"))
            return
        if not isinstance(message, dict) or not isinstance(message.get("type"), str):
            client.push(_error(ERROR_BAD_MESSAGE, "Expected a JSON object with a string 'type'"))
            return

        if message["type"] == "ping":
            client.push(encode("pong", message.get("data")))
        else:
            client.push(_error(ERROR_UNKNOWN_TYPE, f"Unknown message type: {message['type']}"))


def _error(code: str, detail: str) -> str:
    return encode("error", {"error": code, "detail": detail})


def _peer(websocket: WebSocket) -> str:
    client = websocket.client
    return f"{client.host}:{client.port}" if client else "unknown"


async def _close(websocket: WebSocket, code: int) -> None:
    """Закрыть соединение, если его ещё не закрыла ни одна из сторон."""
    if (
        websocket.application_state == WebSocketState.CONNECTED
        and websocket.client_state == WebSocketState.CONNECTED
    ):
        with contextlib.suppress(Exception):
            await websocket.close(code=code)


ws_hub = WebSocketHub()
