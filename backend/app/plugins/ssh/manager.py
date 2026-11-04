"""Менеджер SSH-сессий: открыть, выполнить команду, закрыть, кто смотрит сессию.

* У каждой сессии своё IPC-подключение к воркеру (WorkerManager.open_channel): воркер
  выполняет запросы одного подключения по очереди, а разных параллельно. Долгая команда
  в одной сессии не задерживает другие. Если воркер не принял дополнительное
  подключение, сессии работают через общее основное (по одной команде за раз).
* Сессия воркера принадлежит своему подключению. Если оно оборвалось или воркер упал,
  сессия закрывается и здесь: подписчики получают closed, в шину уходит событие.
* Всё, что происходит в сессии (команда, результат, ошибка), пишется в её буфер вывода
  (buffer.py) и сразу рассылается подписчикам (WebSocket-подключениям UI).
* События ssh.session (opened / closed) публикуются в шину: их будет слушать мониторинг.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from dataclasses import dataclass
from typing import Any, Protocol

import structlog

from app.core.config import settings
from app.core.errors import (
    AppError,
    WorkerCommandError,
    WorkerConnectionError,
    WorkerTimeoutError,
)
from app.core.event_bus import event_bus
from app.keystore import KeyStore
from app.plugins.ssh.buffer import OutputBuffer
from app.plugins.ssh.errors import (
    SSHConnectError,
    SSHExecTimeoutError,
    SSHNotConnectedError,
    SSHSessionNotFoundError,
    SSHTooManySessionsError,
)
from app.plugins.ssh.session import (
    AuthMethod,
    SSHSession,
    WorkerChannel,
    new_session_id,
)
from app.plugins.ssh.wrapper import (
    KILL_AFTER,
    build_command,
    normalize_newlines,
    parse_output,
)
from app.worker import WorkerClient, worker_manager
from app.worker.manager import STATUS_WORKER_EXITED, STATUS_WORKER_STARTED

logger = structlog.get_logger()

SSH_SESSION_EVENT = "ssh.session"

# Причины закрытия сессии: в событии ssh.session и в сообщении closed для UI
CLOSED_BY_USER = "closed"
CLOSED_WORKER_LOST = "worker_connection_lost"
CLOSED_LOST_IN_WORKER = "lost_in_worker"
CLOSED_SHUTDOWN = "shutdown"

# Тексты ошибок воркера, когда сессии с таким id у него нет: сборка 2026-11 и CoreDocs.
SESSION_MISSING_MARKERS = ("Session dont founded", "Not connected", "Session not found")

CLOSE_TIMEOUT = 5.0  # сколько ждать подтверждения ssh_disconnect


class WorkerProvider(Protocol):
    """Откуда менеджер берёт подключения к воркеру (в тестах подменяется)."""

    def shared(self) -> WorkerClient:
        """Основное подключение backend. WorkerConnectionError, если воркера нет."""
        ...

    async def open_channel(self) -> WorkerClient:
        """Новое отдельное подключение. WorkerConnectionError, если не удалось."""
        ...


class WorkerManagerProvider:
    def shared(self) -> WorkerClient:
        client = worker_manager.client
        if client is None or not client.connected:
            raise WorkerConnectionError("Rust worker is not connected")
        return client

    async def open_channel(self) -> WorkerClient:
        return await worker_manager.open_channel()


@dataclass(frozen=True)
class ConnectParams:
    host: str
    port: int
    username: str
    key_id: str | None = None
    key_path: str | None = None
    passphrase: str | None = None  # только вместе с key_path
    password: str | None = None

    @property
    def auth(self) -> AuthMethod:
        if self.key_id is not None:
            return "key_id"
        if self.key_path is not None:
            return "key_path"
        return "password"


@dataclass(frozen=True)
class ExecResult:
    session_id: str
    seq: int  # номер записи с результатом в буфере вывода
    stdout: str
    stderr: str
    exit_code: int
    cwd: str | None
    timed_out: bool
    duration_ms: int


def is_session_missing(stderr: str) -> bool:
    return any(marker in stderr for marker in SESSION_MISSING_MARKERS)


class SSHSessionManager:
    def __init__(self, provider: WorkerProvider | None = None) -> None:
        self.provider: WorkerProvider = provider or WorkerManagerProvider()
        self._sessions: dict[str, SSHSession] = {}
        self._shared: WorkerChannel | None = None
        # Принимает ли воркер дополнительные подключения: None, пока не пробовали
        self._channels_supported: bool | None = None
        self._background: set[asyncio.Task[Any]] = set()

    # -------------------------------------------------------------- lookup

    def get(self, session_id: str) -> SSHSession:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise SSHSessionNotFoundError(f"SSH session '{session_id}' not found") from None

    def resolve(self, session_id: str | None) -> SSHSession:
        """Сессия по id или текущая (последняя открытая), если id не передан."""
        if session_id is not None:
            return self.get(session_id)
        current = self.current
        if current is None:
            raise SSHNotConnectedError("SSH session is not open. Call /api/ssh/connect first.")
        return current

    @property
    def current(self) -> SSHSession | None:
        return next(reversed(self._sessions.values()), None)

    def sessions(self) -> list[SSHSession]:
        return list(self._sessions.values())

    def __len__(self) -> int:
        return len(self._sessions)

    # ---------------------------------------------------------------- open

    async def open(self, params: ConnectParams, keys: KeyStore) -> SSHSession:
        if len(self._sessions) >= settings.SSH_MAX_SESSIONS:
            raise SSHTooManySessionsError(
                f"Too many open SSH sessions (limit {settings.SSH_MAX_SESSIONS}). "
                "Close unused ones first."
            )

        channel = await self._acquire_channel()
        session_id = new_session_id()
        try:
            await self._connect_in_worker(channel, session_id, params, keys)
        except BaseException:
            if channel.dedicated:
                await channel.client.disconnect()
            raise

        session = SSHSession(
            id=session_id,
            host=params.host,
            port=params.port,
            username=params.username,
            auth=params.auth,
            key_id=params.key_id,
            channel=channel,
            buffer=OutputBuffer(settings.SSH_OUTPUT_BUFFER_BYTES),
        )
        self._sessions[session.id] = session
        session.record(
            "opened",
            {"host": session.host, "port": session.port, "username": session.username},
        )
        logger.info(
            "SSH session opened",
            session_id=session.id,
            host=session.host,
            username=session.username,
            auth=session.auth,
            channel="dedicated" if channel.dedicated else "shared",
        )
        await self._publish("opened", session)
        return session

    async def _connect_in_worker(
        self, channel: WorkerChannel, session_id: str, params: ConnectParams, keys: KeyStore
    ) -> None:
        async with channel.lock, contextlib.AsyncExitStack() as stack:
            connection: dict[str, Any] = {
                "host": params.host,
                "port": params.port,
                "username": params.username,
            }
            if params.key_id is not None:
                # Воркер принимает только путь: ключ из хранилища кладётся во временный
                # файл и удаляется сразу после ответа воркера.
                path = await stack.enter_async_context(keys.temporary_key_file(params.key_id))
                connection["key_path"] = str(path)
            elif params.key_path is not None:
                connection["key_path"] = params.key_path
                if params.passphrase is not None:
                    connection["passphrase"] = params.passphrase
            else:
                connection["password"] = params.password

            try:
                await channel.client.request(
                    "ssh_connect", {"connection": connection}, request_id=session_id
                )
            except WorkerCommandError as e:
                raise SSHConnectError(e.stderr) from e
            except WorkerTimeoutError:
                # Воркер может довести подключение до конца уже после таймаута. Отдельное
                # подключение закроется целиком, а в общем сессию надо закрыть явно.
                if not channel.dedicated:
                    self._spawn(self._quiet_disconnect(channel.client, session_id))
                raise

    async def _acquire_channel(self) -> WorkerChannel:
        shared = self.provider.shared()  # 503, если воркер не подключён
        if self._channels_supported is not False:
            try:
                client = await self.provider.open_channel()
            except WorkerConnectionError as e:
                if self._channels_supported is None:
                    self._channels_supported = False
                    logger.warning(
                        "Rust worker does not accept extra IPC connections: SSH sessions "
                        "will share one connection and run commands one at a time",
                        error=e.detail,
                    )
                else:
                    logger.warning("Extra IPC connection failed, using the shared one")
            else:
                self._channels_supported = True
                channel = WorkerChannel(client=client, dedicated=True)
                client.add_disconnect_listener(
                    lambda reason, ch=channel: self._on_channel_lost(ch, reason)
                )
                return channel
        return self._shared_channel(shared)

    def _shared_channel(self, client: WorkerClient) -> WorkerChannel:
        if self._shared is None or self._shared.client is not client:
            if self._shared is not None:
                self._shared.client.remove_disconnect_listener(self._on_shared_lost)
            self._shared = WorkerChannel(client=client, dedicated=False)
            client.add_disconnect_listener(self._on_shared_lost)
        return self._shared

    # ---------------------------------------------------------------- exec

    async def execute(
        self,
        session_id: str | None,
        command: str,
        *,
        timeout: float | None = None,
        track_cwd: bool = True,
        source: str = "api",
        request_id: str | None = None,
    ) -> ExecResult:
        """Выполняет команду в сессии (без session_id в текущей).

        Команды одной сессии выполняются по очереди. С track_cwd команда оборачивается
        (wrapper.py): сохранённая cwd, точный exit_code и лимит на сервере.
        """
        session = self.resolve(session_id)
        command = normalize_newlines(command)
        session.waiting += 1
        acquired = False
        try:
            async with session.channel.lock:
                session.waiting -= 1
                acquired = True
                return await self._execute_locked(
                    session, command, timeout, track_cwd, source, request_id
                )
        finally:
            if not acquired:
                session.waiting -= 1

    async def _execute_locked(
        self,
        session: SSHSession,
        command: str,
        timeout: float | None,
        track_cwd: bool,
        source: str,
        request_id: str | None,
    ) -> ExecResult:
        if session.closed:
            raise SSHNotConnectedError(f"SSH session '{session.id}' is closed")

        limit = timeout or settings.SSH_EXEC_TIMEOUT
        marker = session.marker
        if track_cwd:
            remote_limit = max(1, math.ceil(limit))
            wrapped = build_command(command, session.cwd, marker, remote_limit)
            wait = remote_limit + KILL_AFTER + settings.SSH_EXEC_TIMEOUT_GRACE
        else:
            wrapped = command
            wait = limit

        tag = {"request_id": request_id} if request_id else {}
        session.running = command
        session.record("command", {"command": command, "cwd": session.cwd, "source": source, **tag})
        # Текст команды может содержать пароли и токены: только в DEBUG
        logger.debug("SSH exec", session_id=session.id, command=command, timeout=limit)
        started = time.monotonic()
        try:
            response = await session.channel.client.request(
                "ssh_exec", _session_payload(session, command=wrapped), timeout=wait,
                request_id=session.id,
            )  # fmt: skip
        except WorkerCommandError as e:
            if is_session_missing(e.stderr):
                # Сессии в воркере нет (например, его перезапустили): синхронизируемся.
                self._forget(session, CLOSED_LOST_IN_WORKER)
                raise SSHNotConnectedError(
                    f"SSH session '{session.id}' is no longer open: {e.stderr}"
                ) from e
            self._record_error(session, e, tag)
            raise
        except WorkerTimeoutError as e:
            hint = (
                "The server-side limit did not stop it: the server may lack coreutils `timeout`"
                if track_cwd
                else "Without track_cwd the command is not limited on the server"
            )
            logger.warning("SSH exec timed out without a reply", session_id=session.id, waited=wait)
            error = SSHExecTimeoutError(
                f"No result from the worker within {wait:g}s. {hint}; the command may still be "
                "running and keep the session busy."
            )
            self._record_error(session, error, tag)
            raise error from e
        except WorkerConnectionError as e:
            if session.closed:
                raise SSHNotConnectedError(
                    f"SSH session '{session.id}' was closed ({session.close_reason})"
                ) from e
            raise
        finally:
            session.running = None
        duration_ms = round((time.monotonic() - started) * 1000)

        stdout = str(response.get("stdout") or "")
        exit_code = _as_int(response.get("exit_code"), default=-1)
        timed_out = False
        if track_cwd:
            result = parse_output(stdout, marker, timeout_applied=True)
            stdout = result.stdout
            if result.exit_code is not None:
                # Воркер всегда отдаёт 0 (сборка 2026-11); код из shell точнее.
                exit_code = result.exit_code
            if result.cwd:
                session.cwd = result.cwd
            timed_out = result.timed_out
        stdout = stdout.rstrip("\r\n")
        stderr = str(response.get("stderr") or "").rstrip("\r\n")

        entry = session.record(
            "result",
            {
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": exit_code,
                "cwd": session.cwd,
                "timed_out": timed_out,
                "duration_ms": duration_ms,
                **tag,
            },
        )
        if timed_out:
            logger.info("SSH exec stopped by timeout", session_id=session.id, timeout=limit)
        return ExecResult(
            session_id=session.id,
            seq=entry.seq,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            cwd=session.cwd,
            timed_out=timed_out,
            duration_ms=duration_ms,
        )

    @staticmethod
    def _record_error(session: SSHSession, error: AppError, tag: dict[str, Any]) -> None:
        session.record("error", {"error": error.code, "detail": error.detail, **tag})

    # ------------------------------------------------------ service requests

    async def request_if_idle(
        self,
        session_id: str,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float,
    ) -> dict[str, Any] | None:
        """Служебный запрос к воркеру в сессии (например, monitor_get_values), если
        она свободна. Возвращает payload ответа или None, если сессия занята.

        Ждать нельзя: воркер выполняет запросы подключения по очереди, и опрос встал
        бы за долгой командой, а следующая команда пользователя за опросом. Запрос в
        буфер вывода сессии не пишется.
        """
        session = self.get(session_id)
        lock = session.channel.lock
        if session.closed or lock.locked():
            return None
        async with lock:
            if session.closed:
                return None
            try:
                return await session.channel.client.request(
                    action, _session_payload(session, **(payload or {})), timeout=timeout,
                    request_id=session.id,
                )  # fmt: skip
            except WorkerCommandError as e:
                if is_session_missing(e.stderr):
                    self._forget(session, CLOSED_LOST_IN_WORKER)
                    raise SSHNotConnectedError(
                        f"SSH session '{session.id}' is no longer open: {e.stderr}"
                    ) from e
                raise
            except WorkerConnectionError as e:
                if session.closed:
                    raise SSHNotConnectedError(f"SSH session '{session.id}' was closed") from e
                raise

    # --------------------------------------------------------------- close

    async def close(
        self, session_id: str | None, *, reason: str = CLOSED_BY_USER
    ) -> tuple[SSHSession, str]:
        """Закрывает сессию (без session_id текущую). Возвращает её и ответ воркера.

        Подписчики узнают о закрытии сразу. Если в сессии ещё выполняется команда, её
        отдельное подключение закрывается целиком: воркер сам закроет сессию, а команда
        завершится ошибкой "сессия закрыта".
        """
        session = self.resolve(session_id)
        self._sessions.pop(session.id, None)
        session.mark_closed(reason)
        message = await self._disconnect_in_worker(session)
        logger.info("SSH session closed", session_id=session.id, reason=reason)
        await self._publish("closed", session, reason=reason)
        return session, message

    async def close_all(self, *, reason: str) -> None:
        """Закрывает все сессии без запросов к воркеру: он закроет их сам вместе с
        подключениями (остановка backend, падение воркера)."""
        sessions, self._sessions = list(self._sessions.values()), {}
        for session in sessions:
            session.mark_closed(reason)
            if session.channel.dedicated:
                with contextlib.suppress(Exception):
                    await session.channel.client.disconnect()
            await self._publish("closed", session, reason=reason)
        if sessions:
            logger.info("SSH sessions closed", count=len(sessions), reason=reason)

    async def _disconnect_in_worker(self, session: SSHSession) -> str:
        channel = session.channel
        if channel.dedicated and channel.lock.locked():
            await channel.client.disconnect()
            return "Closed together with its IPC connection while a command was running"
        try:
            async with channel.lock:
                response = await channel.client.request(
                    "ssh_disconnect",
                    _session_payload(session),
                    request_id=session.id,
                    timeout=CLOSE_TIMEOUT,
                )
            return str(response.get("stdout") or "Disconnected").strip()
        except WorkerCommandError as e:
            if is_session_missing(e.stderr):
                return "The session was already closed in the worker"
            return e.stderr
        except WorkerConnectionError:
            return "Rust worker is not connected; the session is already closed"
        except WorkerTimeoutError:
            return "The worker did not confirm in time; the session is closed with its connection"
        finally:
            if channel.dedicated:
                await channel.client.disconnect()

    # ---------------------------------------------------- lost connections

    def _on_channel_lost(self, channel: WorkerChannel, reason: str) -> None:
        """Отдельное подключение сессии оборвалось: воркер уже закрыл её сессию.

        Синхронно, чтобы команда, которая упала вместе с подключением, увидела
        session.closed и вернула понятную ошибку.
        """
        for session in self.sessions():
            if session.channel is channel:
                logger.warning("SSH session lost its worker connection", session_id=session.id)
                self._forget(session, CLOSED_WORKER_LOST)

    def _on_shared_lost(self, reason: str) -> None:
        for session in self.sessions():
            if not session.channel.dedicated:
                logger.warning("SSH session lost its worker connection", session_id=session.id)
                self._forget(session, CLOSED_WORKER_LOST)

    def _forget(self, session: SSHSession, reason: str) -> None:
        if self._sessions.get(session.id) is session:
            del self._sessions[session.id]
        if session.closed:
            return
        session.mark_closed(reason)
        if session.channel.dedicated:
            self._spawn(session.channel.client.disconnect())
        self._spawn(self._publish("closed", session, reason=reason))

    async def on_worker_status(self, event: dict[str, Any]) -> None:
        status = (event.get("payload") or {}).get("status")
        if status == STATUS_WORKER_EXITED:
            await self.close_all(reason=CLOSED_WORKER_LOST)
        if status in (STATUS_WORKER_EXITED, STATUS_WORKER_STARTED):
            self._channels_supported = None  # новый процесс: проверим заново

    # ------------------------------------------------------------- helpers

    async def _publish(self, action: str, session: SSHSession, reason: str | None = None) -> None:
        event = {
            "action": action,
            "session_id": session.id,
            "host": session.host,
            "port": session.port,
            "username": session.username,
        }
        if reason is not None:
            event["reason"] = reason
        await event_bus.publish(SSH_SESSION_EVENT, event)

    async def _quiet_disconnect(self, client: WorkerClient, session_id: str) -> None:
        with contextlib.suppress(Exception):
            await client.request(
                "ssh_disconnect", {"session_id": session_id}, request_id=session_id, timeout=120
            )

    def _spawn(self, awaitable: Any) -> None:
        task = asyncio.ensure_future(awaitable)
        self._background.add(task)
        task.add_done_callback(self._background.discard)


def _session_payload(session: SSHSession, **fields: Any) -> dict[str, Any]:
    # Сейчас воркер ищет сессию по id запроса, а session_id в payload описан в CoreDocs
    # для ssh_disconnect: передаём оба, это работает с любым вариантом.
    return {"session_id": session.id, **fields}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


ssh_manager = SSHSessionManager()
