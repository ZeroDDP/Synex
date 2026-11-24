"""Живой лог контейнера по WebSocket:
ws://127.0.0.1:8000/api/docker/sessions/{session_id}/containers/{container}/logs/ws

Параметры: `tail` (сколько последних строк показать сразу, по умолчанию 200) и `since`
(время последней уже показанной строки: после переподключения UI передаёт `next_since`
и не получает строки повторно).

Первый запрос читает историю. Служебная сессия получает новые строки через docker_logs с follow.

Конверт сообщений тот же, что у /ws: {"type": str, "ts": int (Unix, мс), "data": ...}.

Сервер → UI:
  hello   {protocol, session_id, container, tail, interval_ms} сразу после подключения;
  logs    {lines: [{time, stream, text}], next_since, truncated}: сначала последние
          `tail` строк, потом новые по мере появления. truncated: строк между опросами
          было больше MAX_LINES, самые старые из них пропущены;
  error   {error, detail, fatal}. fatal=false: временная помеха (сессия занята командой
          пользователя, демон Docker не отвечает), опрос продолжается, а сообщение не
          повторяется, пока причина та же. fatal=true: контейнера или Docker нет, после
          сообщения сервер закрывает подключение;
  closed  {reason}: session_closed (SSH-сессию закрыли) или shutdown (плагин или backend
          останавливается), после этого сервер закрывает подключение;
  pong    data из ping.
UI → сервер:
  ping    что угодно, вернётся в pong.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import structlog
from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.core.config import settings
from app.core.errors import AppError, WorkerCommandError
from app.plugins.docker.errors import (
    DockerContainerNotFoundError,
    DockerNotInstalledError,
    DockerPermissionError,
)
from app.plugins.docker.schemas import LogLineInfo
from app.plugins.docker.service import docker_error, docker_request
from app.plugins.ssh.errors import SSHNotConnectedError, SSHSessionNotFoundError
from app.plugins.ssh.manager import SSHSessionManager
from app.ws.hub import encode

logger = structlog.get_logger()

PROTOCOL_VERSION = 1
MAX_LINES = 10_000  # больше строк за один опрос не отдаётся (docker logs --tail)

CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_INTERNAL_ERROR = 1011
CLOSE_NOT_FOUND = 4404
CLOSE_FORBIDDEN = 4403

REASON_SESSION_CLOSED = "session_closed"
REASON_SHUTDOWN = "shutdown"

# Ошибки, после которых опрашивать дальше нет смысла: код закрытия подключения
FATAL: dict[type[AppError], int] = {
    DockerContainerNotFoundError: CLOSE_NOT_FOUND,
    DockerNotInstalledError: CLOSE_NOT_FOUND,
    DockerPermissionError: CLOSE_FORBIDDEN,
}
SESSION_GONE = (SSHSessionNotFoundError, SSHNotConnectedError)

_streams: set[asyncio.Task[Any]] = set()


async def fetch_logs(
    manager: SSHSessionManager,
    session_id: str,
    container: str,
    *,
    tail: int | None,
    since: str | None,
    wait: float | None = None,
) -> tuple[list[LogLineInfo], str | None]:
    """Строки лога контейнера и время последней из них (для следующего since)."""
    batch = await docker_request(
        manager,
        session_id,
        "docker_logs",
        {"container": container, "tail": tail, "since": since, "follow": False},
        wait=wait,
    )
    return [LogLineInfo(**line) for line in batch["lines"]], batch["next_since"] or since


def active_streams() -> int:
    return len(_streams)


async def close_streams() -> None:
    """Закрыть все живые логи (выгрузка плагина, остановка backend)."""
    tasks = list(_streams)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def serve_log_socket(
    websocket: WebSocket,
    manager: SSHSessionManager,
    session_id: str,
    container: str,
    tail: int,
    since: str | None,
) -> None:
    await websocket.accept()
    task = asyncio.current_task()
    assert task is not None
    _streams.add(task)
    interval = settings.DOCKER_LOGS_INTERVAL
    log = logger.bind(session_id=session_id, container=container)
    log.info("Docker log stream opened", streams=len(_streams))
    code = CLOSE_NORMAL
    send = _Sender(websocket)
    receiver = asyncio.create_task(_receive(websocket, send))
    try:
        await send(
            "hello",
            {
                "protocol": PROTOCOL_VERSION,
                "session_id": session_id,
                "container": container,
                "tail": tail,
                "interval_ms": round(interval * 1000),
            },
        )
        code = await _poll(send, receiver, manager, session_id, container, tail, since)
    except asyncio.CancelledError:
        # Плагин выгружают или backend останавливается: UI узнаёт причину и переподключится
        code = CLOSE_GOING_AWAY
        with contextlib.suppress(Exception):
            async with asyncio.timeout(1):
                await send("closed", {"reason": REASON_SHUTDOWN})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("Docker log stream failed", error=repr(e))
        code = CLOSE_INTERNAL_ERROR
    finally:
        _streams.discard(task)
        receiver.cancel()
        with contextlib.suppress(BaseException):
            await receiver
        await _close(websocket, code)
        log.info("Docker log stream closed", code=code, streams=len(_streams))


async def _poll(
    send: _Sender,
    receiver: asyncio.Task[None],
    manager: SSHSessionManager,
    session_id: str,
    container: str,
    tail: int,
    since: str | None,
) -> int:
    """Опрос до отключения UI. Возвращает код закрытия подключения."""
    interval = settings.DOCKER_LOGS_INTERVAL
    first = True
    problem: str | None = None  # код последней временной ошибки: не повторяем её
    while not receiver.done():
        # tail=0: старые строки не нужны, но нужна точка отсчёта. Без неё следующий
        # опрос (без --since) принёс бы весь старый лог. Берём время последней строки.
        skip_old = first and tail == 0 and since is None
        limit = 1 if skip_old else tail if first else MAX_LINES
        try:
            lines, since = await fetch_logs(
                manager,
                session_id,
                container,
                tail=limit,
                since=since,
                # Занятую сессию ждём не дольше периода опроса: следующий тик попробует снова
                wait=min(interval, settings.SSH_SERVICE_WAIT),
            )
            if skip_old:
                lines = []
        except SESSION_GONE:
            await send("closed", {"reason": REASON_SESSION_CLOSED})
            return CLOSE_NORMAL
        except AppError as e:
            fatal = FATAL.get(type(e))
            if fatal is not None:
                await send("error", _error(e, fatal=True))
                return fatal
            if e.code != problem:
                problem = e.code
                await send("error", _error(e, fatal=False))
        else:
            problem = None
            if lines or first:
                await send(
                    "logs",
                    {
                        "lines": [
                            {"time": line.time, "stream": line.stream, "text": line.text}
                            for line in lines
                        ],
                        "next_since": since,
                        "truncated": not first and len(lines) >= MAX_LINES,
                    },
                )
            first = False
            if manager.get(session_id).channel.native_cancel:
                try:
                    companion = await manager.open_companion(session_id, "docker_logs")
                    if companion is not None:

                        async def forward(channel_session):
                            nonlocal since
                            client = channel_session.channel.client
                            follow = {
                                "container": container,
                                "since": since,
                                "tail": MAX_LINES,
                                "follow": True,
                            }
                            async with contextlib.aclosing(
                                client.progress(
                                    "docker_logs", follow, request_id=channel_session.id
                                )
                            ) as batches:
                                async for payload in batches:
                                    batch = json.loads(payload["stdout"])
                                    await send("logs", {**batch, "truncated": False})
                                    since = batch["next_since"] or since

                        task = asyncio.create_task(forward(companion))
                        try:
                            await asyncio.wait(
                                (receiver, task), return_when=asyncio.FIRST_COMPLETED
                            )
                            if task.done():
                                await task
                        finally:
                            task.cancel()
                            await manager.close_companion(companion)
                            await asyncio.gather(task, return_exceptions=True)
                except SESSION_GONE:
                    await send("closed", {"reason": REASON_SESSION_CLOSED})
                    return CLOSE_NORMAL
                except WorkerCommandError as exc:
                    error = docker_error(exc)
                    fatal = FATAL.get(type(error))
                    await send("error", _error(error, fatal=fatal is not None))
                    if fatal is not None:
                        return fatal
                except AppError:
                    # Retry from the last delivered cursor on the next poll.
                    pass
        # Отключение UI обрывает паузу сразу
        await asyncio.wait({receiver}, timeout=interval)
    return CLOSE_NORMAL


async def _receive(websocket: WebSocket, send: _Sender) -> None:
    """Сообщения UI: ping. Завершается, когда UI отключился."""
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        try:
            data = json.loads(message.get("text") or "")
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("type") == "ping":
            await send("pong", data.get("data"))
        else:
            await send(
                "error",
                {"error": "bad_message", "detail": "Only ping is accepted", "fatal": False},
            )


def _error(error: AppError, *, fatal: bool) -> dict[str, Any]:
    return {"error": error.code, "detail": error.detail, "fatal": fatal}


class _Sender:
    """Отправка в подключение по одному сообщению: строки лога и pong идут из разных
    задач."""

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self._lock = asyncio.Lock()

    async def __call__(self, message_type: str, data: Any) -> None:
        async with self._lock:
            await self.websocket.send_text(encode(message_type, data))


async def _close(websocket: WebSocket, code: int) -> None:
    if (
        websocket.application_state == WebSocketState.CONNECTED
        and websocket.client_state == WebSocketState.CONNECTED
    ):
        with contextlib.suppress(Exception):
            await websocket.close(code=code)
