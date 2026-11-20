"""WebSocket терминала: ws://127.0.0.1:8000/api/ssh/sessions/{session_id}/ws?since=N

Одно подключение смотрит одну сессию; одну сессию могут смотреть несколько подключений
(например, две вкладки UI). Конверт сообщений тот же, что у /ws:
{"type": str, "ts": int (Unix, мс), "data": ...}.

Сервер → UI:
  hello   {protocol, session: <как GET /api/ssh/sessions/{id}>, replay: {since, count,
          gap, first_seq, last_seq}} сразу после подключения;
  output  запись буфера {seq, ts, kind, ...}: сначала пропущенные после since, потом
          новые. kind: opened | command | result | error;
  closed  {reason}: сессию закрыли, после этого сервер закрывает подключение;
  error   {error, detail, request_id?, ...}: сообщение UI не разобрано или команда не
          запустилась (например, сессия уже закрыта; программе нужен терминал:
          ssh_interactive_command с program и hint);
  interrupt {status, signal, command}: ответ на interrupt (как POST .../interrupt);
  pong    data из ping.
UI → сервер:
  exec    {command, timeout?, track_cwd?, allow_interactive?, id?}: id вернётся в записях
          как request_id;
  interrupt  прервать идущую команду (Ctrl+C);
  close   закрыть сессию (для всех её подключений);
  ping    что угодно, вернётся в pong.

Вывод терминала нельзя молча терять, поэтому очередь клиента не урезается, как у /ws.
Если клиент не успевает читать (в очереди больше MAX_PENDING_BYTES), подключение
закрывается кодом 1013: UI переподключается с since=<последний seq> и получает
пропущенное из буфера. Отключение UI сессию не закрывает: команды продолжают
выполняться, их вывод ждёт в буфере.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import structlog
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import Field, ValidationError
from starlette.websockets import WebSocketState

from app.core.errors import AppError, WorkerCommandError
from app.plugins.ssh.buffer import OutputEntry
from app.plugins.ssh.errors import SSHExecTimeoutError, SSHSessionNotFoundError
from app.plugins.ssh.manager import CLOSED_SHUTDOWN, SSHSessionManager
from app.plugins.ssh.schemas import SSHCommandRequest
from app.plugins.ssh.session import SSHSession
from app.plugins.ssh.terminal import run_user_command
from app.ws.hub import encode

logger = structlog.get_logger()

PROTOCOL_VERSION = 1
MAX_PENDING_BYTES = 8 * 1024 * 1024

CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_INTERNAL_ERROR = 1011
CLOSE_TRY_AGAIN_LATER = 1013
CLOSE_SESSION_NOT_FOUND = 4404

# Ошибки команды, которые уже записаны в буфер сессии (и разосланы всем подписчикам)
RECORDED_ERRORS = (SSHExecTimeoutError, WorkerCommandError)

_running: set[asyncio.Task[Any]] = set()  # команды живут дольше подключения UI


class ExecMessage(SSHCommandRequest):
    id: str | None = Field(default=None, max_length=64, description="Вернётся как request_id")


class SessionSocket:
    """Одно WebSocket-подключение к сессии. Подписчик сессии (session.Subscriber)."""

    def __init__(self, websocket: WebSocket, manager: SSHSessionManager, session: SSHSession):
        self.websocket = websocket
        self.manager = manager
        self.session = session
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.pending_bytes = 0
        self.close_code: int | None = None

    # ---------------------------------------------------- Subscriber

    def deliver(self, entry: OutputEntry) -> None:
        self.push("output", entry.as_dict())

    def session_closed(self, reason: str) -> None:
        self.push("closed", {"reason": reason})
        self.finish(CLOSE_GOING_AWAY if reason == CLOSED_SHUTDOWN else CLOSE_NORMAL)

    # ------------------------------------------------------- queue

    def push(self, message_type: str, data: Any = None) -> None:
        if self.close_code is not None:
            return
        text = encode(message_type, data)
        self.pending_bytes += len(text)
        if self.pending_bytes > MAX_PENDING_BYTES:
            logger.warning("SSH WebSocket client is too slow, closing", session_id=self.session.id)
            self.finish(CLOSE_TRY_AGAIN_LATER)
            return
        self.queue.put_nowait(text)

    def finish(self, code: int) -> None:
        """Закрыть подключение после того, что уже стоит в очереди (кроме 1013)."""
        if self.close_code is None:
            self.close_code = code
            self.queue.put_nowait(None)

    # ---------------------------------------------------- serving

    async def send_loop(self) -> None:
        while True:
            text = await self.queue.get()
            if text is None or self.close_code == CLOSE_TRY_AGAIN_LATER:
                return
            self.pending_bytes -= len(text)
            await self.websocket.send_text(text)

    async def receive_loop(self) -> None:
        while True:
            message = await self.websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            self.handle(message.get("text"))

    def handle(self, text: str | None) -> None:
        try:
            message = json.loads(text) if text is not None else None
        except ValueError:
            message = None
        if not isinstance(message, dict) or not isinstance(message.get("type"), str):
            self.error("bad_message", "Expected a JSON object with a string 'type'")
            return

        kind, data = message["type"], message.get("data")
        if kind == "ping":
            self.push("pong", data)
        elif kind == "exec":
            try:
                request = ExecMessage.model_validate(data)
            except ValidationError as e:
                self.error("bad_message", _validation_detail(e))
                return
            self._spawn(self._execute(request))
        elif kind == "interrupt":
            self._spawn(self._interrupt())
        elif kind == "close":
            self._spawn(self._close_session())
        else:
            self.error("unknown_type", f"Unknown message type: {kind}")

    def error(
        self,
        code: str,
        detail: str,
        request_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        data: dict[str, Any] = {"error": code, "detail": detail, **(extra or {})}
        if request_id is not None:
            data["request_id"] = request_id
        self.push("error", data)

    async def _execute(self, request: ExecMessage) -> None:
        try:
            await run_user_command(
                self.manager, self.session.id, request, source="ws", request_id=request.id
            )
        except RECORDED_ERRORS:
            pass  # уже в буфере: подписчики получили запись kind=error
        except AppError as e:
            self.error(e.code, e.detail, request.id, e.extra)
        except Exception as e:
            logger.exception("SSH exec from WebSocket failed", session_id=self.session.id)
            self.error("internal_error", repr(e), request.id)

    async def _interrupt(self) -> None:
        try:
            result = await self.manager.interrupt(self.session.id)
        except AppError as e:
            self.error(e.code, e.detail)
            return
        except Exception as e:
            logger.exception("SSH interrupt from WebSocket failed", session_id=self.session.id)
            self.error("internal_error", repr(e))
            return
        self.push(
            "interrupt",
            {"status": result.status, "signal": result.signal, "command": result.command},
        )

    async def _close_session(self) -> None:
        with contextlib.suppress(SSHSessionNotFoundError):
            await self.manager.close(self.session.id)

    @staticmethod
    def _spawn(awaitable: Any) -> None:
        task = asyncio.ensure_future(awaitable)
        _running.add(task)
        task.add_done_callback(_running.discard)


async def serve_session_socket(
    websocket: WebSocket, manager: SSHSessionManager, session_id: str, since: int = 0
) -> None:
    await websocket.accept()
    try:
        session = manager.get(session_id)
    except SSHSessionNotFoundError as e:
        await websocket.send_text(encode("error", {"error": e.code, "detail": e.detail}))
        await _close(websocket, CLOSE_SESSION_NOT_FOUND)
        return

    socket = SessionSocket(websocket, manager, session)
    # Подписка и снимок буфера без await между ними: ни одна запись не потеряется
    replay = session.attach(socket, since)
    socket.push(
        "hello",
        {
            "protocol": PROTOCOL_VERSION,
            "session": _jsonable(session.info()),
            "replay": {
                "since": since,
                "count": len(replay.entries),
                "gap": replay.gap,
                "first_seq": replay.first_seq,
                "last_seq": replay.last_seq,
            },
        },
    )
    for entry in replay.entries:
        socket.deliver(entry)
    logger.info("SSH WebSocket attached", session_id=session.id, attached=len(session.subscribers))

    sender = asyncio.create_task(socket.send_loop())
    receiver = asyncio.create_task(socket.receive_loop())
    try:
        await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
    finally:
        session.detach(socket)
        sender.cancel()
        receiver.cancel()
        results = await asyncio.gather(sender, receiver, return_exceptions=True)
        failed = [
            r
            for r in results
            if isinstance(r, Exception) and not isinstance(r, WebSocketDisconnect)
        ]
        for error in failed:
            logger.warning("SSH WebSocket failed", session_id=session.id, error=repr(error))
        code = CLOSE_INTERNAL_ERROR if failed else (socket.close_code or CLOSE_NORMAL)
        await _close(websocket, code)
        logger.info("SSH WebSocket detached", session_id=session.id, code=code)


async def _close(websocket: WebSocket, code: int) -> None:
    if (
        websocket.application_state == WebSocketState.CONNECTED
        and websocket.client_state == WebSocketState.CONNECTED
    ):
        with contextlib.suppress(Exception):
            await websocket.close(code=code)


def _jsonable(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v.isoformat() if hasattr(v, "isoformat") else v for k, v in data.items()}


def _validation_detail(error: ValidationError) -> str:
    first = error.errors()[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "data"
    return f"{location}: {first.get('msg')}"
