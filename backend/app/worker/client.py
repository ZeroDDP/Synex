"""IPC-клиент к synex_worker (протокол описан в CoreDocs, раздел 4).

Фрейм:   [4 байта: длина payload, Big-Endian][N байт: MessagePack map]
Запрос:  {"id": str, "action": str, "payload": {...}}
Ответ:   {"id": str, "status": "ok" | "error", "payload": {stdout, stderr, exit_code}}
         (sftp_download_file сборки 2026-11 отвечает "success" вместо "ok")

Как ведёт себя воркер на самом деле (проверено на сборке от 2026-11-03):
  * `id` запроса одновременно ключ SSH-сессии: ssh_connect с id X создаёт сессию X,
    ssh_exec / monitor_get_values / ssh_disconnect с тем же id работают с ней.
    Поле payload.session_id воркер не читает. Ответ приходит с тем же id.
  * Запросы одного IPC-подключения выполняются строго по очереди, разных подключений
    параллельно. Сессии принадлежат подключению: другое их не видит, а при закрытии
    подключения воркер закрывает его сессии.

Поэтому несколько запросов могут ждать ответа с одинаковым id. Ответы сопоставляются
с ними по порядку отправки (очередь на каждый id). Запрос, который не дождался ответа
(таймаут или отмена), остаётся в очереди отменённым: его поздний ответ выбрасывается
и не достаётся следующему запросу с тем же id.

Транспорт:
    Windows      Named Pipe через overlapped I/O (ProactorEventLoop), запасной: TCP 127.0.0.1
    Linux/macOS  Unix Domain Socket, запасной: TCP 127.0.0.1

Воркер слушает ровно один транспорт (pipe ИЛИ tcp), поэтому клиент пробует оба.

Почему pipe открывается не через open(): на Windows open() даёт синхронный хэндл,
а система сериализует все операции над синхронным хэндлом. Висящий read() в фоновом
потоке блокирует write() из другого потока, и запрос не уходит вообще (deadlock).
loop.create_pipe_connection() открывает pipe в overlapped-режиме, в котором чтение и
запись идут независимо, а сверху получаются обычные StreamReader/StreamWriter, как у TCP.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import sys
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

import msgpack
import structlog

from app.core.config import settings
from app.core.errors import (
    WorkerCommandError,
    WorkerConnectionError,
    WorkerError,
    WorkerTimeoutError,
)

# Успешный ответ воркера; "success" отдаёт sftp_download_file (сборка 2026-11)
OK_STATUSES = frozenset({"ok", "success"})

logger = structlog.get_logger()

HEADER_SIZE = 4
MAX_FRAME_SIZE = 64 * 1024 * 1024  # защита от мусорного заголовка
LOCALHOST = "127.0.0.1"  # воркер слушает TCP только на loopback

Opener = Callable[[], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]
DisconnectListener = Callable[[str], Awaitable[None] | None]
# Транспорт, который выбрал воркер: строка или функция, которую клиент спрашивает
# перед каждой попыткой (воркер может сначала объявить pipe, а потом уйти на TCP).
Prefer = str | Callable[[], str | None] | None


def encode_frame(message: dict[str, Any]) -> bytes:
    data = msgpack.packb(message, use_bin_type=True)
    if len(data) > MAX_FRAME_SIZE:
        raise WorkerError(f"Request too large: {len(data)} bytes")
    return len(data).to_bytes(HEADER_SIZE, "big") + data


def make_request(
    action: str, payload: dict[str, Any] | None = None, request_id: str | None = None
) -> dict[str, Any]:
    return {"id": request_id or uuid.uuid4().hex, "action": action, "payload": payload or {}}


class WorkerClient:
    """Одно IPC-соединение с воркером. Ответы сопоставляются с запросами по id."""

    def __init__(
        self,
        *,
        pipe_name: str | None = None,
        socket_path: str | None = None,
        tcp_port: int | None = None,
        request_timeout: float | None = None,
    ) -> None:
        self._pipe_name = pipe_name or settings.RUST_IPC_PIPE_NAME
        self._socket_path = socket_path or settings.ipc_socket_path
        self._tcp_port = tcp_port or settings.RUST_IPC_TCP_FALLBACK_PORT
        self._request_timeout = request_timeout or settings.RUST_IPC_REQUEST_TIMEOUT

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        # id -> ожидающие ответа запросы в порядке отправки (см. docstring модуля)
        self._progress: dict[str, asyncio.Queue] = {}
        self._pending: dict[str, deque[asyncio.Future[dict[str, Any]]]] = {}
        self._disconnect_listeners: list[DisconnectListener] = []
        self._background: set[asyncio.Task] = set()
        self._connected = False
        self._closing = False
        self._transport = "none"
        self._warned_no_proactor = False

    # ------------------------------------------------------------------ connect

    async def connect(
        self,
        *,
        timeout: float | None = None,
        retry_delay: float = 0.3,
        prefer: Prefer = None,
    ) -> None:
        """Подключается к воркеру, повторяя попытки, пока не истечёт timeout.

        prefer: "pipe" | "unix" | "tcp", если заранее известно, что слушает воркер
        (менеджер узнаёт это по строке "Listening on ..." в stdout воркера). Можно
        передать функцию: она вызывается перед каждой попыткой.
        """
        async with self._connect_lock:
            if self._connected:
                return
            await self._connect_locked(timeout, retry_delay, prefer)

    async def _connect_locked(
        self, timeout: float | None, retry_delay: float, prefer: Prefer
    ) -> None:
        timeout = settings.RUST_IPC_CONNECT_TIMEOUT if timeout is None else timeout
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        errors: dict[str, str] = {}

        while True:
            current = prefer() if callable(prefer) else prefer
            for name, opener in self._candidates(current):
                per_try = max(0.5, min(3.0, deadline - loop.time()))
                try:
                    reader, writer = await asyncio.wait_for(opener(), timeout=per_try)
                except NotImplementedError as e:
                    errors[name] = str(e)
                    continue
                except (OSError, TimeoutError) as e:
                    errors[name] = repr(e)
                    continue
                self._attach(reader, writer, name)
                return

            if loop.time() + retry_delay >= deadline:
                break
            await asyncio.sleep(retry_delay)

        details = "; ".join(f"{k}: {v}" for k, v in errors.items()) or "no transports tried"
        raise WorkerConnectionError(f"Cannot connect to Rust worker ({details})")

    def _candidates(self, prefer: str | None) -> list[tuple[str, Opener]]:
        if sys.platform == "win32":
            items: list[tuple[str, Opener]] = [("pipe", self._open_pipe), ("tcp", self._open_tcp)]
        else:
            items = [("unix", self._open_unix), ("tcp", self._open_tcp)]
        if prefer:
            preferred = [item for item in items if item[0] == prefer]
            if preferred:
                return preferred
        return items

    async def _open_pipe(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        loop = asyncio.get_running_loop()
        create_pipe = getattr(loop, "create_pipe_connection", None)
        if create_pipe is None:
            if not self._warned_no_proactor:
                self._warned_no_proactor = True
                logger.warning(
                    "Named pipe needs ProactorEventLoop; current loop cannot open it. "
                    "uvicorn switches to SelectorEventLoop on Windows with --reload/--workers. "
                    "Trying TCP fallback only.",
                    loop=type(loop).__name__,
                )
            raise NotImplementedError(f"{type(loop).__name__} has no create_pipe_connection")

        reader = asyncio.StreamReader(loop=loop)
        protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
        transport, _ = await create_pipe(lambda: protocol, self._pipe_name)
        writer = asyncio.StreamWriter(transport, protocol, reader, loop)
        return reader, writer

    async def _open_unix(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_unix_connection(self._socket_path)

    async def _open_tcp(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection(LOCALHOST, self._tcp_port)

    def _attach(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, transport: str
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._transport = transport
        self._connected = True
        self._closing = False
        self._read_task = asyncio.create_task(self._read_loop(reader), name="rust-ipc-reader")
        target = {
            "pipe": self._pipe_name,
            "unix": self._socket_path,
            "tcp": f"{LOCALHOST}:{self._tcp_port}",
        }[transport]
        logger.info("IPC connected", transport=transport, target=target)

    # --------------------------------------------------------------- disconnect

    async def disconnect(self) -> None:
        self._closing = True
        was_connected = self._connected
        self._connected = False
        self._reader = None  # read-loop увидит, что он устарел, и не тронет состояние

        writer, self._writer = self._writer, None
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)

        task, self._read_task = self._read_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        self._fail_pending(WorkerConnectionError("IPC connection closed"))
        self._transport = "none"
        if was_connected:
            logger.info("IPC disconnected")

    def add_disconnect_listener(self, listener: DisconnectListener) -> None:
        """listener(reason) вызывается при НЕожиданном обрыве (не при disconnect())."""
        self._disconnect_listeners.append(listener)

    def remove_disconnect_listener(self, listener: DisconnectListener) -> None:
        with contextlib.suppress(ValueError):
            self._disconnect_listeners.remove(listener)

    # ------------------------------------------------------------------ request

    async def request(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Отправляет action и возвращает payload ответа со status="ok".

        request_id: id запроса. Для действий с SSH-сессией это id сессии, иначе
        генерируется случайный.

        WorkerCommandError: воркер ответил status="error".
        WorkerTimeoutError: ответа нет дольше timeout.
        WorkerConnectionError: нет соединения или оно оборвалось.
        """
        response = await self.request_raw(action, payload, timeout=timeout, request_id=request_id)
        body = response.get("payload")
        if not isinstance(body, dict):
            body = {}

        if response.get("status") == "cancelled" and action == "ssh_exec":
            return {**body, "cancelled": True}
        if response.get("status") == "cancelled":
            raise WorkerCommandError(action, "cancelled", payload=body)
        if response.get("status") in OK_STATUSES:
            return body

        raise WorkerCommandError(
            action=action,
            stderr=str(body.get("stderr") or "Unknown worker error"),
            stdout=str(body.get("stdout") or ""),
            exit_code=_as_int(body.get("exit_code"), default=1),
            payload=body,
        )

    async def request_raw(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Отправляет запрос и возвращает ответ воркера целиком, без разбора status."""
        message = make_request(action, payload, request_id)
        frame = encode_frame(message)
        msg_id = message["id"]

        # Проверка соединения и регистрация future идут без await между ними:
        # при обрыве read-loop гарантированно увидит и уронит этот future.
        writer = self._writer
        if not self._connected or writer is None:
            raise WorkerConnectionError("Not connected to Rust worker")
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending.setdefault(msg_id, deque()).append(future)

        sent = False
        try:
            async with self._write_lock:
                writer.write(frame)
                sent = True
                await writer.drain()
        except (ConnectionError, OSError, RuntimeError) as e:
            self._forget(msg_id, future)
            raise WorkerConnectionError(f"Failed to send '{action}' to Rust worker: {e!r}") from e
        except BaseException:
            # Отмена, пока запрос ждал очереди на запись: воркер его не получит, и место
            # в очереди ответов ему не нужно. Если кадр уже записан, ответ придёт.
            if sent:
                future.cancel()
            else:
                self._forget(msg_id, future)
            raise

        wait = self._request_timeout if timeout is None else timeout
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=wait)
        except TimeoutError:
            raise WorkerTimeoutError(
                f"Rust worker did not answer '{action}' within {wait:g}s"
            ) from None
        finally:
            # Запрос уже у воркера, ответ придёт позже. Отменённый future остаётся в
            # очереди своего id и заберёт этот поздний ответ на себя.
            future.cancel()

    async def progress(self, action: str, payload: dict, *, request_id: str):
        """Intermediate replies on a dedicated channel. The caller owns its lifetime."""
        queue = asyncio.Queue(maxsize=8)
        self._progress[request_id] = queue
        request = asyncio.create_task(
            self.request(action, payload, timeout=float("inf"), request_id=request_id)
        )
        item = None
        try:
            while True:
                if not queue.empty():
                    yield queue.get_nowait()
                    continue
                if request.done():
                    await request
                    return
                item = asyncio.create_task(queue.get())
                await asyncio.wait((item, request), return_when=asyncio.FIRST_COMPLETED)
                if item.done():
                    yield item.result()
                else:
                    item.cancel()
                    await asyncio.gather(item, return_exceptions=True)
        finally:
            self._progress.pop(request_id, None)
            request.cancel()
            if item is not None:
                item.cancel()
                await asyncio.gather(item, return_exceptions=True)
            await asyncio.gather(request, return_exceptions=True)

    # ------------------------------------------------------------------- status

    async def is_connected(self) -> bool:
        return self._connected

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def transport(self) -> str:
        return self._transport

    def status(self) -> dict[str, Any]:
        return {
            "connected": self._connected,
            "transport": self._transport,
            "pending_requests": sum(
                not future.done() for queue in self._pending.values() for future in queue
            ),
        }

    # --------------------------------------------------------------- internals

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        reason = "connection closed by worker"
        try:
            while True:
                header = await reader.readexactly(HEADER_SIZE)
                length = int.from_bytes(header, "big")
                if length > MAX_FRAME_SIZE:
                    reason = f"frame too large ({length} bytes), stream is out of sync"
                    logger.error("IPC protocol error", reason=reason)
                    return
                body = await reader.readexactly(length)
                message = self._dispatch(body)
                if message is not None:
                    queue = self._progress.get(message["id"])
                    if queue is not None:
                        await queue.put(message["payload"])
        except asyncio.IncompleteReadError:
            pass
        except ConnectionError as e:
            # Воркер завершился или упал: на Unix socket и pipe это часто RST, а не EOF.
            # Это не ошибка клиента: выход воркера логирует и обрабатывает менеджер.
            reason = f"connection reset by worker ({type(e).__name__})"
        except asyncio.CancelledError:
            reason = "reader cancelled"
            raise
        except Exception as e:
            reason = f"reader error: {e!r}"
            logger.error("IPC reader failed", error=repr(e))
        finally:
            self._on_connection_lost(reader, reason)

    def _dispatch(self, body: bytes) -> dict | None:
        try:
            message = msgpack.unpackb(body, raw=False)
        except Exception as e:
            logger.error("IPC: cannot decode MessagePack frame", error=repr(e), size=len(body))
            return

        if not isinstance(message, dict):
            logger.error(
                "IPC: frame is not a map. Worker must serialize with rmp_serde::to_vec_named",
                got=type(message).__name__,
            )
            return

        msg_id = message.get("id")
        if message.get("status") == "progress":
            return message
        queue = self._pending.get(msg_id) if isinstance(msg_id, str) else None
        if queue:
            future = queue.popleft()
            if not queue:
                del self._pending[msg_id]
            if not future.done():
                future.set_result(message)
            else:
                logger.debug("IPC: late reply to an abandoned request dropped", id=msg_id)
            return

        # Например id="bad_msgpack": воркер не смог разобрать наш запрос.
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        logger.warning(
            "IPC: response without a waiting request (late reply or worker-side error)",
            id=msg_id,
            status=message.get("status"),
            stderr=payload.get("stderr"),
        )

    def _on_connection_lost(self, reader: asyncio.StreamReader, reason: str) -> None:
        if reader is not self._reader:
            return  # это старое соединение: disconnect() уже всё убрал

        expected = self._closing
        self._connected = False
        self._transport = "none"
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
        self._fail_pending(WorkerConnectionError(f"Lost connection to Rust worker: {reason}"))

        if expected:
            return
        logger.warning("IPC connection lost", reason=reason)
        for listener in list(self._disconnect_listeners):
            try:
                result = listener(reason)
                if inspect.isawaitable(result):
                    self._spawn(result)
            except Exception as e:
                logger.error("IPC disconnect listener failed", error=repr(e))

    def _spawn(self, awaitable: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(awaitable)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _forget(self, msg_id: str, future: asyncio.Future[dict[str, Any]]) -> None:
        """Убирает запрос, который так и не ушёл воркеру."""
        queue = self._pending.get(msg_id)
        if queue is None:
            return
        with contextlib.suppress(ValueError):
            queue.remove(future)
        if not queue:
            del self._pending[msg_id]

    def _fail_pending(self, error: WorkerConnectionError) -> None:
        pending, self._pending = self._pending, {}
        for queue in pending.values():
            for future in queue:
                if not future.done():
                    future.set_exception(error)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
