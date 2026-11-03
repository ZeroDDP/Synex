"""Действия с SSH-сессиями через synex_worker: открыть, выполнить команду, закрыть.

Маршруты /api/ssh только разбирают запрос и вызывают эти функции, поэтому тот же код
можно звать и не из HTTP (например, из WebSocket).
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from dataclasses import dataclass
from typing import Any

import structlog

from app.core.config import settings
from app.core.errors import (
    WorkerCommandError,
    WorkerConnectionError,
    WorkerTimeoutError,
)
from app.core.locks import LoopLock
from app.keystore import KeyStore
from app.plugins.ssh.errors import SSHConnectError, SSHExecTimeoutError, SSHNotConnectedError
from app.plugins.ssh.session import AuthMethod, SSHSession, new_session_id, ssh_sessions
from app.plugins.ssh.wrapper import KILL_AFTER, build_command, parse_output
from app.worker import WorkerClient

logger = structlog.get_logger()

# Тексты ошибок воркера, когда сессии с таким id у него нет: сборка 2026-11 и CoreDocs.
SESSION_MISSING_MARKERS = ("Session dont founded", "Not connected", "Session not found")

# Воркер выполняет запросы одного IPC-подключения строго по очереди, а backend держит
# одно подключение на все сессии. Поэтому запросы к воркеру идут по одному: иначе
# таймаут команды тикал бы, пока она стоит в очереди воркера за командой другой сессии.
ipc_lock = LoopLock()

_background: set[asyncio.Task[Any]] = set()


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
    stdout: str
    stderr: str
    exit_code: int
    cwd: str | None
    timed_out: bool
    duration_ms: int


def is_session_missing(stderr: str) -> bool:
    return any(marker in stderr for marker in SESSION_MISSING_MARKERS)


async def connect(worker: WorkerClient, keys: KeyStore, params: ConnectParams) -> SSHSession:
    """Открывает новую сессию. Её id становится id запросов к воркеру."""
    session_id = new_session_id()
    async with ipc_lock, contextlib.AsyncExitStack() as stack:
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
            response = await worker.request(
                "ssh_connect", {"connection": connection}, request_id=session_id
            )
        except WorkerCommandError as e:
            raise SSHConnectError(e.stderr) from e
        except WorkerTimeoutError:
            # Воркер может довести подключение до конца уже после нашего таймаута.
            _close_in_background(worker, session_id)
            raise

    session = SSHSession(
        id=session_id,
        host=params.host,
        port=params.port,
        username=params.username,
        auth=params.auth,
        key_id=params.key_id,
    )
    ssh_sessions.add(session)
    logger.debug("Worker connect reply", session_id=session_id, stdout=response.get("stdout"))
    return session


async def execute(
    worker: WorkerClient,
    session_id: str | None,
    command: str,
    *,
    timeout: float | None = None,
    track_cwd: bool = True,
) -> ExecResult:
    """Выполняет команду в сессии (без session_id в текущей).

    С track_cwd команда оборачивается (wrapper.py): сохранённая cwd, точный exit_code и
    лимит на сервере. По лимиту команда прерывается, ответ приходит с timed_out=True.
    """
    limit = timeout or settings.SSH_EXEC_TIMEOUT

    async with ipc_lock:
        session = ssh_sessions.resolve(session_id)
        marker = session.marker
        if track_cwd:
            remote_limit = max(1, math.ceil(limit))
            wrapped = build_command(command, session.cwd, marker, remote_limit)
            wait = remote_limit + KILL_AFTER + settings.SSH_EXEC_TIMEOUT_GRACE
        else:
            wrapped = command
            wait = limit

        # Текст команды может содержать пароли и токены: только в DEBUG
        logger.debug(
            "SSH exec", session_id=session.id, command=command, cwd=session.cwd, timeout=limit
        )
        started = time.monotonic()
        try:
            response = await worker.request(
                "ssh_exec", _session_payload(session, command=wrapped), timeout=wait,
                request_id=session.id,
            )  # fmt: skip
        except WorkerCommandError as e:
            if is_session_missing(e.stderr):
                # Сессии в воркере нет (например, его перезапустили): синхронизируемся.
                ssh_sessions.remove(session.id, reason="unknown to the worker")
                raise SSHNotConnectedError(
                    f"SSH session '{session.id}' is no longer open: {e.stderr}"
                ) from e
            raise
        except WorkerTimeoutError as e:
            # Сессия в воркере может быть жива, состояние не трогаем.
            hint = (
                "The server-side limit did not stop it: the server may lack coreutils `timeout`"
                if track_cwd
                else "Without track_cwd the command is not limited on the server"
            )
            logger.warning("SSH exec timed out without a reply", session_id=session.id, waited=wait)
            raise SSHExecTimeoutError(
                f"No result from the worker within {wait:g}s. {hint}; the command may still be "
                "running and keep the worker busy."
            ) from e
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

    if timed_out:
        logger.info(
            "SSH exec stopped by timeout",
            session_id=session.id,
            timeout=limit,
            duration_ms=duration_ms,
        )
    return ExecResult(
        session_id=session.id,
        stdout=stdout.rstrip("\r\n"),
        stderr=str(response.get("stderr") or "").rstrip("\r\n"),
        exit_code=exit_code,
        cwd=session.cwd,
        timed_out=timed_out,
        duration_ms=duration_ms,
    )


async def disconnect(worker: WorkerClient, session_id: str | None) -> tuple[SSHSession, str]:
    """Закрывает сессию (без session_id текущую). Возвращает сессию и ответ воркера."""
    async with ipc_lock:
        session = ssh_sessions.resolve(session_id)
        try:
            response = await worker.request(
                "ssh_disconnect", _session_payload(session), request_id=session.id
            )
            message = str(response.get("stdout") or "Disconnected").strip()
        except WorkerCommandError as e:
            if not is_session_missing(e.stderr):
                raise
            message = "The session was already closed in the worker"
        except WorkerConnectionError:
            # Без IPC-подключения сессий в воркере уже нет: он закрывает их сам.
            message = "Rust worker is not connected; the session is already closed"
        finally:
            ssh_sessions.remove(session.id, reason="disconnect requested")
    return session, message


def _session_payload(session: SSHSession, **fields: Any) -> dict[str, Any]:
    # Сейчас воркер ищет сессию по id запроса, а session_id в payload описан в CoreDocs
    # для ssh_disconnect: передаём оба, это работает с любым вариантом.
    return {"session_id": session.id, **fields}


def _close_in_background(worker: WorkerClient, session_id: str) -> None:
    async def close() -> None:
        with contextlib.suppress(Exception):
            await worker.request(
                "ssh_disconnect", {"session_id": session_id}, request_id=session_id, timeout=120
            )

    task = asyncio.ensure_future(close())
    _background.add(task)
    task.add_done_callback(_background.discard)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
