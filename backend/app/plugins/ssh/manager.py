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
* Плагины (docker, git) выполняют в сессии свои служебные команды через run_service:
  они не попадают в буфер вывода и не меняют cwd сессии. Действия воркера кроме команд
  (например, sftp_*) плагины выполняют через request.
* Для долгой операции (передача файла) плагин открывает служебную сессию к тому же
  серверу (open_companion): у неё своё IPC-подключение, поэтому терминал, метрики и
  другие запросы основной сессии во время передачи не ждут. Служебные сессии не видны
  в списке, не считаются в SSH_MAX_SESSIONS и закрываются вместе с основной.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import re
import time
from collections.abc import Callable
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
    SSHConnectTimeoutError,
    SSHExecTimeoutError,
    SSHHostKeyMismatchError,
    SSHHostKeyUnknownError,
    SSHNotConnectedError,
    SSHSessionBusyError,
    SSHSessionNotFoundError,
    SSHTooManySessionsError,
)
from app.plugins.ssh.known_hosts import get_known_hosts, server_name
from app.plugins.ssh.session import (
    AuthMethod,
    CompanionSession,
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
CLOSED_EXEC_TIMEOUT = "exec_timeout"

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


@dataclass(frozen=True)
class ServiceResult:
    """Результат служебной команды плагина (run_service)."""

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    duration_ms: int


def is_session_missing(stderr: str) -> bool:
    return any(marker in stderr for marker in SESSION_MISSING_MARKERS)


HOST_KEY_UNKNOWN = "host_key_unknown"
HOST_KEY_MISMATCH = "host_key_mismatch"
_FINGERPRINT_IN_TEXT = re.compile(r"SHA256:[A-Za-z0-9+/]+")


def connect_error(
    error: WorkerCommandError, params: ConnectParams, expected: str | None
) -> AppError:
    """Ошибка ssh_connect воркера для API: ключ сервера не подтверждён или изменился
    (TOFU, 409 с отпечатками в host_key), иначе ssh_connect_failed."""
    stderr = error.stderr
    body = error.payload
    found = _FINGERPRINT_IN_TEXT.findall(stderr)
    server = server_name(params.host, params.port)
    if HOST_KEY_UNKNOWN in stderr:
        fingerprint = _text(body.get("fingerprint")) or (found[-1] if found else "")
        key_type = _text(body.get("key_type"))
        return SSHHostKeyUnknownError(
            f"The key of {server} is not trusted yet: {fingerprint} ({key_type or 'unknown type'})."
            " Make sure it is the key of your server, trust it (POST /api/ssh/known-hosts) and "
            "connect again",
            {"host": params.host, "port": params.port, "fingerprint": fingerprint,
             "key_type": key_type},
        )  # fmt: skip
    if HOST_KEY_MISMATCH in stderr:
        fingerprint = _text(body.get("fingerprint")) or (found[-1] if found else "")
        trusted = _text(body.get("expected_fingerprint")) or expected or (found[0] if found else "")
        return SSHHostKeyMismatchError(
            f"The key of {server} has changed: trusted {trusted}, the server shows "
            f"{fingerprint}. Not connecting: this may be an attack (man in the middle) or the "
            "server was reinstalled. If the change is expected, trust the new key with "
            "replace=true (POST /api/ssh/known-hosts)",
            {"host": params.host, "port": params.port, "fingerprint": fingerprint,
             "key_type": _text(body.get("key_type")), "expected_fingerprint": trusted},
        )  # fmt: skip
    return SSHConnectError(stderr)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


class SSHSessionManager:
    def __init__(self, provider: WorkerProvider | None = None) -> None:
        self.provider: WorkerProvider = provider or WorkerManagerProvider()
        self._sessions: dict[str, SSHSession] = {}
        self._shared: WorkerChannel | None = None
        # Принимает ли воркер дополнительные подключения: None, пока не пробовали
        self._channels_supported: bool | None = None
        self._background: set[asyncio.Task[Any]] = set()
        self._opening = 0  # подключения, которые ещё идут: тоже занимают место в лимите
        self._companions: dict[str, set[CompanionSession]] = {}  # по id основной сессии

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

    async def open(
        self, params: ConnectParams, keys: KeyStore, *, connection_id: str | None = None
    ) -> SSHSession:
        # Считаются и подключения, которые ещё идут: иначе одновременные запросы прошли
        # бы проверку все разом и открыли больше SSH_MAX_SESSIONS сессий
        if len(self._sessions) + self._opening >= settings.SSH_MAX_SESSIONS:
            raise SSHTooManySessionsError(
                f"Too many open SSH sessions (limit {settings.SSH_MAX_SESSIONS}). "
                "Close unused ones first."
            )

        # Отпечаток, который пользователь уже подтвердил (TOFU): без него воркер не
        # подключится и вернёт ключ сервера для подтверждения
        fingerprint = await get_known_hosts().fingerprint(params.host, params.port)

        self._opening += 1
        try:
            channel = await self._acquire_channel()
            session_id = new_session_id()
            try:
                host_key = await self._connect_in_worker(
                    channel, session_id, params, keys, fingerprint=fingerprint
                )
            except BaseException:
                if channel.dedicated:
                    await channel.client.disconnect()
                raise
        finally:
            self._opening -= 1

        session = SSHSession(
            id=session_id,
            host=params.host,
            port=params.port,
            username=params.username,
            auth=params.auth,
            key_id=params.key_id,
            connection_id=connection_id,
            host_key=host_key,
            channel=channel,
            buffer=OutputBuffer(settings.SSH_OUTPUT_BUFFER_BYTES),
            connect_params=params,
            keys=keys,
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
        self,
        channel: WorkerChannel,
        session_id: str,
        params: ConnectParams,
        keys: KeyStore,
        *,
        fingerprint: str | None,
    ) -> dict[str, str] | None:
        """ssh_connect в воркере. Возвращает ключ сервера {"fingerprint", "key_type"}
        (None, если сборка воркера его не сообщает)."""
        async with channel.lock, contextlib.AsyncExitStack() as stack:
            connection: dict[str, Any] = {
                "host": params.host,
                "port": params.port,
                "username": params.username,
            }
            if fingerprint is not None:
                connection["host_key_fingerprint"] = fingerprint
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

            timeout = settings.SSH_CONNECT_TIMEOUT
            try:
                body = await channel.client.request(
                    "ssh_connect",
                    {"connection": connection},
                    request_id=session_id,
                    timeout=timeout,
                )
            except WorkerCommandError as e:
                raise connect_error(e, params, fingerprint) from e
            except WorkerTimeoutError as e:
                # Воркер может довести подключение до конца уже после таймаута. Отдельное
                # подключение закроется целиком, а в общем сессию надо закрыть явно.
                if not channel.dedicated:
                    self._spawn(self._quiet_disconnect(channel.client, session_id))
                raise SSHConnectTimeoutError(
                    f"No SSH connection to {params.host}:{params.port} within {timeout:g}s: "
                    "the server does not answer (wrong address or port, firewall, or it is "
                    "down). The limit is SSH_CONNECT_TIMEOUT"
                ) from e
        key = body.get("fingerprint")
        if not isinstance(key, str) or not key:
            return None
        key_type = body.get("key_type")
        return {"fingerprint": key, "key_type": key_type if isinstance(key_type, str) else ""}

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
            response = await self._exec_in_worker(
                session,
                wrapped,
                wait,
                limited=track_cwd,
                on_error=lambda error: self._record_error(session, error, tag),
            )
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

    async def _exec_in_worker(
        self,
        session: SSHSession,
        command: str,
        wait: float,
        *,
        limited: bool,
        on_error: Callable[[AppError], None] | None = None,
    ) -> dict[str, Any]:
        """ssh_exec в воркере (lock канала уже взят) и перевод его ошибок в ошибки сессии.

        limited: команда обёрнута с лимитом на сервере (wrapper.py). on_error получает
        ошибку команды до того, как она будет выброшена (запись в буфер вывода).
        """
        try:
            return await session.channel.client.request(
                "ssh_exec", _session_payload(session, command=command), timeout=wait,
                request_id=session.id,
            )  # fmt: skip
        except WorkerCommandError as e:
            if is_session_missing(e.stderr):
                # Сессии в воркере нет (например, его перезапустили): синхронизируемся.
                self._forget(session, CLOSED_LOST_IN_WORKER)
                raise SSHNotConnectedError(
                    f"SSH session '{session.id}' is no longer open: {e.stderr}"
                ) from e
            if on_error is not None:
                on_error(e)
            raise
        except WorkerTimeoutError as e:
            hint = (
                "The server-side limit did not stop it: the server may lack coreutils `timeout`"
                if limited
                else "Without track_cwd the command is not limited on the server"
            )
            logger.warning("SSH exec timed out without a reply", session_id=session.id, waited=wait)
            if session.channel.dedicated:
                # Воркер выполняет запросы подключения по очереди и отменять команду не
                # умеет: все следующие команды сессии встали бы за зависшей. Сессия
                # закрывается вместе со своим подключением, UI откроет новую.
                outcome = "The session was closed so that it does not stay blocked; open a new one."
            else:
                outcome = "The command may still be running and keep the session busy."
            error = SSHExecTimeoutError(
                f"No result from the worker within {wait:g}s. {hint}. {outcome}"
            )
            if on_error is not None:
                on_error(error)
            if session.channel.dedicated:
                self._forget(session, CLOSED_EXEC_TIMEOUT)
            raise error from e
        except WorkerConnectionError as e:
            if session.closed:
                raise SSHNotConnectedError(
                    f"SSH session '{session.id}' was closed ({session.close_reason})"
                ) from e
            raise

    @staticmethod
    def _record_error(session: SSHSession, error: AppError, tag: dict[str, Any]) -> None:
        session.record("error", {"error": error.code, "detail": error.detail, **tag})

    # ------------------------------------------------------ plugin commands

    async def run_service(
        self,
        session_id: str,
        command: str,
        *,
        timeout: float | None = None,
        wait: float | None = None,
        name: str = "service",
    ) -> ServiceResult:
        """Служебная команда плагина (docker, git) в сессии.

        В отличие от execute: не пишется в буфер вывода (терминал её не видит), не
        меняет cwd сессии и выполняется из домашней папки. Как и execute: точный код
        возврата, stdin из /dev/null и лимит timeout на сервере (wrapper.py).

        Если в сессии выполняется команда пользователя, ждёт не дольше wait
        (SSH_SERVICE_WAIT), потом SSHSessionBusyError (409): воркер выполняет команды
        сессии по очереди. Плагину с долгими командами лучше открыть свою сессию.
        """
        session = self.get(session_id)
        command = normalize_newlines(command)
        wait_for_lock = settings.SSH_SERVICE_WAIT if wait is None else wait
        if not await session.channel.lock.acquire(timeout=wait_for_lock):
            raise SSHSessionBusyError(
                f"SSH session '{session.id}' is busy with another command for more than "
                f"{wait_for_lock:g}s. Try again later or open a separate session for {name}."
            )
        try:
            if session.closed:
                raise SSHNotConnectedError(f"SSH session '{session.id}' is closed")
            remote_limit = max(1, math.ceil(timeout or settings.SSH_EXEC_TIMEOUT))
            wrapped = build_command(command, None, session.marker, remote_limit)
            reply_wait = remote_limit + KILL_AFTER + settings.SSH_EXEC_TIMEOUT_GRACE
            logger.debug("SSH service command", session_id=session.id, plugin=name, command=command)
            started = time.monotonic()
            response = await self._exec_in_worker(session, wrapped, reply_wait, limited=True)
        finally:
            session.channel.lock.release()

        duration_ms = round((time.monotonic() - started) * 1000)
        result = parse_output(
            str(response.get("stdout") or ""), session.marker, timeout_applied=True
        )
        exit_code = result.exit_code
        if exit_code is None:
            exit_code = _as_int(response.get("exit_code"), default=-1)
        return ServiceResult(
            stdout=result.stdout,
            stderr=str(response.get("stderr") or "").rstrip("\r\n"),
            exit_code=exit_code,
            timed_out=result.timed_out,
            duration_ms=duration_ms,
        )

    # ------------------------------------------------------ service requests

    async def request(
        self,
        session_id: str,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float,
        wait: float | None = None,
        name: str = "service",
    ) -> dict[str, Any]:
        """Действие воркера (например, sftp_list_dir) в сессии от имени плагина.

        Как run_service: в буфер вывода не пишется, занятую командой пользователя сессию
        ждёт не дольше wait (SSH_SERVICE_WAIT), потом SSHSessionBusyError. wait=0: не
        ждать вовсе. Ошибку действия (status="error") отдаёт как WorkerCommandError.
        Если воркер не ответил за timeout, отдельное подключение сессии закрывается:
        отменять действия воркер не умеет, и сессия осталась бы занятой.
        """
        session = self.get(session_id)
        wait_for_lock = settings.SSH_SERVICE_WAIT if wait is None else wait
        if not await session.channel.lock.acquire(timeout=wait_for_lock):
            raise SSHSessionBusyError(
                f"SSH session '{session.id}' is busy with another command for more than "
                f"{wait_for_lock:g}s. Try again later or open a separate session for {name}."
            )
        try:
            if session.closed:
                raise SSHNotConnectedError(f"SSH session '{session.id}' is closed")
            return await self._request_in_worker(session, action, payload or {}, timeout)
        finally:
            session.channel.lock.release()

    async def _request_in_worker(
        self, session: SSHSession, action: str, payload: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        try:
            return await session.channel.client.request(
                action, _session_payload(session, **payload), timeout=timeout,
                request_id=session.id,
            )  # fmt: skip
        except WorkerCommandError as e:
            if is_session_missing(e.stderr):
                self._forget(session, CLOSED_LOST_IN_WORKER)
                raise SSHNotConnectedError(
                    f"SSH session '{session.id}' is no longer open: {e.stderr}"
                ) from e
            raise
        except WorkerTimeoutError:
            logger.warning("Worker action timed out", session_id=session.id, action=action)
            if session.channel.dedicated:
                self._forget(session, CLOSED_EXEC_TIMEOUT)
            raise
        except WorkerConnectionError as e:
            if session.closed:
                raise SSHNotConnectedError(
                    f"SSH session '{session.id}' was closed ({session.close_reason})"
                ) from e
            raise

    # ------------------------------------------------------ companion sessions

    async def open_companion(self, session_id: str, purpose: str) -> CompanionSession | None:
        """Служебная сессия к серверу сессии session_id со своим IPC-подключением.

        None, если открыть её нельзя: воркер не принимает дополнительные подключения,
        или сессию открыли до того, как backend стал запоминать способ входа. Тогда
        плагин работает в основной сессии. SSHConnectError и другие ошибки подключения
        выбрасываются (например, пароль сменили на сервере).
        """
        parent = self.get(session_id)
        if parent.connect_params is None or parent.keys is None:
            return None
        if self._channels_supported is False:
            return None
        try:
            client = await self.provider.open_channel()
        except WorkerConnectionError as e:
            logger.warning("Companion session needs an extra IPC connection", error=e.detail)
            return None
        channel = WorkerChannel(client=client, dedicated=True)
        companion = CompanionSession(
            id=new_session_id(), parent_id=parent.id, purpose=purpose, channel=channel
        )
        # Тот же ключ, с которым подключилась основная сессия: служебная идёт к тому же
        # серверу, и его ключ уже проверен
        fingerprint = parent.host_key["fingerprint"] if parent.host_key else None
        if fingerprint is None:
            params = parent.connect_params
            fingerprint = await get_known_hosts().fingerprint(params.host, params.port)
        try:
            await self._connect_in_worker(
                channel, companion.id, parent.connect_params, parent.keys, fingerprint=fingerprint
            )
        except BaseException:
            await client.disconnect()
            raise
        if parent.closed:  # основную закрыли, пока подключались
            await client.disconnect()
            raise SSHNotConnectedError(f"SSH session '{parent.id}' is closed")
        self._companions.setdefault(parent.id, set()).add(companion)
        client.add_disconnect_listener(lambda reason, c=companion: self._companion_lost(c))
        logger.info(
            "Companion SSH session opened",
            session_id=parent.id,
            companion_id=companion.id,
            purpose=purpose,
        )
        return companion

    async def companion_request(
        self,
        companion: CompanionSession,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        """Действие воркера в служебной сессии. Если её закрыли (отмена операции или
        закрытие основной сессии), SSHNotConnectedError."""
        if companion.closed:
            raise SSHNotConnectedError(f"Companion session of '{companion.parent_id}' is closed")
        async with companion.channel.lock:
            try:
                return await companion.channel.client.request(
                    action, {"session_id": companion.id, **(payload or {})}, timeout=timeout,
                    request_id=companion.id,
                )  # fmt: skip
            except WorkerConnectionError as e:
                if companion.closed:
                    raise SSHNotConnectedError(
                        f"Companion session of '{companion.parent_id}' was closed"
                    ) from e
                raise
            except WorkerTimeoutError:
                await self.close_companion(companion)
                raise

    async def close_companion(self, companion: CompanionSession) -> None:
        """Закрыть служебную сессию. Идущее в ней действие воркера обрывается: воркер
        закрывает сессию вместе с её подключением."""
        companion.closed = True
        companions = self._companions.get(companion.parent_id)
        if companions is not None:
            companions.discard(companion)
            if not companions:
                del self._companions[companion.parent_id]
        with contextlib.suppress(Exception):
            await companion.channel.client.disconnect()

    def companions(self, session_id: str) -> list[CompanionSession]:
        return list(self._companions.get(session_id, ()))

    def _companion_lost(self, companion: CompanionSession) -> None:
        if not companion.closed:
            logger.info("Companion SSH session lost its worker connection", id=companion.id)
        companion.closed = True
        companions = self._companions.get(companion.parent_id)
        if companions is not None:
            companions.discard(companion)
            if not companions:
                del self._companions[companion.parent_id]

    def _drop_companions(self, session: SSHSession) -> None:
        for companion in self._companions.pop(session.id, set()):
            companion.closed = True
            self._spawn(companion.channel.client.disconnect())

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
        self._drop_companions(session)
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
            self._drop_companions(session)
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
        self._drop_companions(session)
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
            "connection_id": session.connection_id,
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
