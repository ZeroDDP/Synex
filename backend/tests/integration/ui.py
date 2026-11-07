"""Клиент, который ведёт себя как UI: HTTP API и оба WebSocket backend на настоящем
uvicorn, с заголовком Origin dev-сервера UI (как браузер со страницы UI)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from websockets.asyncio.client import ClientConnection, connect

UI_ORIGIN = "http://localhost:5173"


class Stream:
    """Одно WebSocket-подключение UI: конверт {type, ts, data}."""

    def __init__(self, ws: ClientConnection) -> None:
        self.ws = ws

    async def send(self, message_type: str, data: Any = None) -> None:
        await self.ws.send(json.dumps({"type": message_type, "data": data}))

    async def next(self, timeout: float = 30.0) -> dict:
        return json.loads(await asyncio.wait_for(self.ws.recv(), timeout))

    async def wait_for(
        self,
        message_type: str,
        predicate: Callable[[Any], bool] = lambda data: True,
        timeout: float = 30.0,
    ) -> dict:
        """Первое сообщение нужного типа, для data которого predicate истинен;
        остальные (например, метрики между делом) пропускаются."""
        async with asyncio.timeout(timeout):
            while True:
                message = json.loads(await self.ws.recv())
                if message["type"] == message_type and predicate(message["data"]):
                    return message

    async def wait_for_all(
        self,
        expected: list[tuple[str, Callable[[Any], bool]]],
        timeout: float = 30.0,
    ) -> list[dict]:
        """Сообщения на каждое ожидание из expected в любом порядке прихода (например,
        потеря сессии и статусы воркера после его падения). Ответ в порядке expected."""
        found: list[dict | None] = [None] * len(expected)
        async with asyncio.timeout(timeout):
            while None in found:
                message = json.loads(await self.ws.recv())
                for index, (message_type, predicate) in enumerate(expected):
                    if (
                        found[index] is None
                        and message["type"] == message_type
                        and predicate(message["data"])
                    ):
                        found[index] = message
                        break
        return [message for message in found if message is not None]


class UI:
    def __init__(self, ws_base: str) -> None:
        self.ws_base = ws_base
        self.api = httpx.AsyncClient(
            base_url=ws_base.replace("ws://", "http://"),
            headers={"Origin": UI_ORIGIN},
            timeout=90,
        )

    async def close(self) -> None:
        await self.api.aclose()

    async def open_session(self, body: dict) -> str:
        response = await self.api.post("/api/ssh/sessions", json=body)
        assert response.status_code == 201, response.text
        return response.json()["session_id"]

    async def exec(self, session_id: str, command: str, **extra: Any) -> dict:
        response = await self.api.post(
            f"/api/ssh/sessions/{session_id}/exec", json={"command": command, **extra}
        )
        assert response.status_code == 200, response.text
        return response.json()

    @asynccontextmanager
    async def events(self) -> AsyncIterator[Stream]:
        """/ws: метрики, события сессий и воркера. hello уже прочитан."""
        async with connect(f"{self.ws_base}/ws", origin=UI_ORIGIN) as ws:
            stream = Stream(ws)
            hello = await stream.next()
            assert hello["type"] == "hello", hello
            yield stream

    @asynccontextmanager
    async def terminal(
        self, session_id: str, since: int | None = None
    ) -> AsyncIterator[tuple[Stream, dict]]:
        """Терминал сессии: (поток, data из hello)."""
        query = "" if since is None else f"?since={since}"
        url = f"{self.ws_base}/api/ssh/sessions/{session_id}/ws{query}"
        async with connect(url, origin=UI_ORIGIN) as ws:
            stream = Stream(ws)
            hello = await stream.next()
            assert hello["type"] == "hello", hello
            yield stream, hello["data"]

    async def run_in_terminal(self, terminal: Stream, command: str, **extra: Any) -> dict:
        """exec через WebSocket терминала: ждёт запись result (или error) этой команды."""
        request_id = f"r{id(command)}-{asyncio.get_running_loop().time()}"
        await terminal.send("exec", {"command": command, "id": request_id, **extra})
        message = await terminal.wait_for(
            "output",
            lambda data: data.get("request_id") == request_id and data["kind"] != "command",
            timeout=90,
        )
        return message["data"]
