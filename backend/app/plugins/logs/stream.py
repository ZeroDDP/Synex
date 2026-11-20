"""Живой лог сервера по WebSocket: журнал systemd или файл.

  ws://127.0.0.1:8000/api/logs/servers/{server}/journal/ws?unit=nginx.service&priority=err
  ws://127.0.0.1:8000/api/logs/servers/{server}/file/ws?path=/var/log/syslog

Воркер выполняет команду целиком и отдаёт вывод в конце, поэтому `journalctl -f` и
`tail -f` через него не идут: новые строки собираются опросом раз в LOGS_INTERVAL.
Протокол от этого не зависит: когда ядро научится отдавать поток, изменится только
источник строк.

Конверт сообщений тот же, что у /ws и живого лога Docker: {"type", "ts", "data"}.

Сервер -> UI:
  hello    {protocol, server, session_id, source, interval_ms, lines, companion} сразу
           после подключения; companion: опрос идёт в своей служебной SSH-сессии;
  entries  (журнал) {entries: [...], cursor, truncated}: сначала последние `lines` записей,
           потом новые. cursor: курсор последней записи, с ним UI переподключается
           (`after`) и не получает записи повторно. truncated: новых записей было больше
           LOGS_MAX_LINES, остальные придут следующим сообщением;
  lines    (файл) {lines: [str], offset, inode, size, rotated, skipped, truncated}:
           сначала последние `lines` строк, потом новые. offset и inode: с ними UI
           переподключается и продолжает с того же места. rotated: файл начался заново
           (logrotate), skipped: сколько байт пропущено (лог пишет быстрее, чем читаем);
  error    {error, detail, fatal}. fatal=false: временная помеха (сессия занята, файл
           пропал при ротации, journalctl не ответил), опрос продолжается, а сообщение не
           повторяется, пока причина та же. fatal=true: источника нет или фильтр
           неверный, после сообщения сервер закрывает подключение;
  closed   {reason}: server_disconnected (закрыли последнюю SSH-сессию к серверу) или
           shutdown (плагин или backend останавливается), потом сервер закрывает
           подключение;
  pong     data из ping.
UI -> сервер:
  ping     что угодно, вернётся в pong.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.core.config import settings
from app.core.errors import AppError
from app.plugins.logs import journal, service
from app.plugins.logs.errors import (
    LogFileDeniedError,
    LogFileIsDirectoryError,
    LogFileNotFoundError,
    LogsBadFilterError,
    LogsJournalUnavailableError,
)
from app.plugins.logs.files import Chunk
from app.ws.hub import encode

logger = structlog.get_logger()

PROTOCOL_VERSION = 1

CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_INTERNAL_ERROR = 1011
CLOSE_FORBIDDEN = 4403
CLOSE_NOT_FOUND = 4404
CLOSE_BAD_REQUEST = 4422

REASON_SERVER_DISCONNECTED = "server_disconnected"
REASON_SHUTDOWN = "shutdown"

# Ошибки, после которых опрашивать дальше нет смысла: код закрытия подключения.
# Для файла это так только при первом чтении: потом файл может пропасть при ротации.
FATAL: dict[type[AppError], int] = {
    LogsJournalUnavailableError: CLOSE_NOT_FOUND,
    LogsBadFilterError: CLOSE_BAD_REQUEST,
    LogFileNotFoundError: CLOSE_NOT_FOUND,
    LogFileDeniedError: CLOSE_FORBIDDEN,
    LogFileIsDirectoryError: CLOSE_BAD_REQUEST,
}

_streams: set[asyncio.Task[Any]] = set()

Send = Callable[[str, Any], Awaitable[None]]


def active_streams() -> int:
    return len(_streams)


async def close_streams() -> None:
    """Закрыть все живые логи (выгрузка плагина, остановка backend)."""
    tasks = list(_streams)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


class JournalSource:
    name = "journal"

    def __init__(self, query: journal.JournalQuery, lines: int, after: str | None) -> None:
        self.query = query
        self.lines = lines
        self.cursor = after
        self.resume = after is not None

    async def first(self, runner: service.LogRunner) -> tuple[str, dict[str, Any]]:
        entries: list[journal.JournalEntry] = []
        if self.resume:
            return await self.next(runner, initial=True)  # type: ignore[return-value]
        if self.lines > 0:
            entries = await service.journal_page(runner, self.query, self.lines)
        if entries:
            self.cursor = entries[-1].cursor
        else:
            # Старых записей нет или они не нужны: новые считаем от конца журнала
            self.cursor = await service.journal_tail_cursor(runner)
        return "entries", self._message(entries, truncated=False)

    async def next(
        self, runner: service.LogRunner, *, initial: bool = False
    ) -> tuple[str, dict[str, Any]] | None:
        limit = settings.LOGS_MAX_LINES
        if self.cursor is None:
            # Журнал был пуст: всё, что появилось, новое
            entries = await service.journal_page(runner, self.query, limit)
        else:
            entries = await service.journal_follow(runner, self.query, self.cursor, limit)
        if entries:
            self.cursor = entries[-1].cursor
        elif self.cursor is None:
            self.cursor = await service.journal_tail_cursor(runner)
        if not entries and not initial:
            return None
        return "entries", self._message(entries, truncated=len(entries) >= limit)

    def _message(self, entries: list[journal.JournalEntry], *, truncated: bool) -> dict[str, Any]:
        return {
            "entries": [entry.as_dict() for entry in entries],
            "cursor": self.cursor,
            "truncated": truncated,
        }


class FileSource:
    name = "file"

    def __init__(
        self,
        path: str,
        lines: int,
        grep: str | None,
        offset: int | None,
        inode: str | None,
    ) -> None:
        self.path = path
        self.lines = lines
        self.grep = grep
        self.offset = offset
        self.inode = inode

    async def first(self, runner: service.LogRunner) -> tuple[str, dict[str, Any]]:
        if self.offset is not None:
            # Переподключение: продолжаем с того места, где UI остановился
            message = await self.next(runner, first=True)
            assert message is not None
            return message
        chunk = await service.read_tail(runner, self.path, self.lines, self.grep)
        self.offset = chunk.offset
        self.inode = chunk.inode
        lines = chunk.lines[-self.lines :] if self.lines else []
        return "lines", self._message(chunk, lines, truncated=False)

    async def next(
        self, runner: service.LogRunner, *, first: bool = False
    ) -> tuple[str, dict[str, Any]] | None:
        assert self.offset is not None
        chunk = await service.read_follow(runner, self.path, self.offset, self.inode)
        error = service.classify_file_status(chunk.status, self.path)
        if error is not None:
            raise error
        lines = chunk.lines
        if self.grep:
            needle = self.grep.casefold()
            lines = [line for line in lines if needle in line.casefold()]
        rotated = chunk.state == "rotated"
        self.offset = chunk.offset
        self.inode = chunk.inode
        if not (lines or rotated or chunk.skipped or first):
            return None
        limit = settings.LOGS_MAX_LINES
        return "lines", self._message(
            chunk,
            lines[-limit:],
            truncated=len(lines) > limit,
            rotated=rotated,
            skipped=chunk.skipped,
        )

    def _message(
        self,
        chunk: Chunk,
        lines: list[str],
        *,
        truncated: bool,
        rotated: bool = False,
        skipped: int = 0,
    ) -> dict[str, Any]:
        return {
            "lines": lines,
            "offset": self.offset,
            "inode": self.inode,
            "size": chunk.size,
            "rotated": rotated,
            "skipped": skipped,
            "truncated": truncated,
        }


Source = JournalSource | FileSource


async def serve_stream(
    websocket: WebSocket,
    runner_factory: Callable[[], service.LogRunner],
    source: Source,
    server: str,
    lines: int,
) -> None:
    await websocket.accept()
    task = asyncio.current_task()
    assert task is not None
    _streams.add(task)
    log = logger.bind(server=server, source=source.name)
    send = _Sender(websocket)
    receiver = asyncio.create_task(_receive(websocket, send))
    runner: service.LogRunner | None = None
    code = CLOSE_NORMAL
    try:
        try:
            runner = runner_factory()
        except service.GONE as e:
            await send("error", _error(e, fatal=True))
            code = CLOSE_NOT_FOUND
            return
        log.info("Log stream opened", session_id=runner.session_id, streams=len(_streams))
        code = await _poll(send, receiver, runner, source, server, lines)
    except asyncio.CancelledError:
        code = CLOSE_GOING_AWAY
        with contextlib.suppress(Exception):
            async with asyncio.timeout(1):
                await send("closed", {"reason": REASON_SHUTDOWN})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("Log stream failed", error=repr(e))
        code = CLOSE_INTERNAL_ERROR
    finally:
        _streams.discard(task)
        receiver.cancel()
        with contextlib.suppress(BaseException):
            await receiver
        if runner is not None:
            with contextlib.suppress(Exception):
                await runner.close()
        await _close(websocket, code)
        log.info("Log stream closed", code=code, streams=len(_streams))


async def _poll(
    send: Send,
    receiver: asyncio.Task[None],
    runner: service.LogRunner,
    source: Source,
    server: str,
    lines: int,
) -> int:
    interval = settings.LOGS_INTERVAL
    await runner.prepare()
    await send(
        "hello",
        {
            "protocol": PROTOCOL_VERSION,
            "server": server,
            "session_id": runner.session_id,
            "source": source.name,
            "interval_ms": round(interval * 1000),
            "lines": lines,
            "companion": runner.companion,
        },
    )
    started = False
    problem: str | None = None  # код последней временной ошибки: не повторяем её
    while not receiver.done():
        try:
            if not started:
                message = await source.first(runner)
                started = True
            else:
                message = await source.next(runner)
        except service.GONE:
            await send("closed", {"reason": REASON_SERVER_DISCONNECTED})
            return CLOSE_NORMAL
        except AppError as e:
            # Файл может пропасть при ротации: после первого чтения это временная помеха
            always = isinstance(e, LogsBadFilterError | LogsJournalUnavailableError)
            fatal = FATAL.get(type(e)) if always or not started else None
            if fatal is not None:
                await send("error", _error(e, fatal=True))
                return fatal
            if e.code != problem:
                problem = e.code
                await send("error", _error(e, fatal=False))
        else:
            problem = None
            if message is not None:
                await send(*message)
        await asyncio.wait({receiver}, timeout=interval)
    return CLOSE_NORMAL


async def _receive(websocket: WebSocket, send: Send) -> None:
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
    """Отправка в подключение по одному сообщению: строки и pong идут из разных задач."""

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
