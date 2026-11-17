from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Path, Query, WebSocket
from pydantic import SecretStr

from app.api.deps import Keys
from app.plugins.ssh.known_hosts import KnownHost, get_known_hosts
from app.plugins.ssh.manager import ConnectParams, ExecResult, SSHSessionManager, ssh_manager
from app.plugins.ssh.schemas import (
    SESSION_ID_PATTERN,
    KnownHostChange,
    KnownHostInfo,
    KnownHostRequest,
    SSHCommandRequest,
    SSHConnectRequest,
    SSHConnectResponse,
    SSHDisconnectRequest,
    SSHDisconnectResponse,
    SSHExecRequest,
    SSHExecResponse,
    SSHOutputResponse,
    SSHSessionInfo,
    SSHStatusResponse,
)
from app.plugins.ssh.session import SSHSession
from app.plugins.ssh.ws import serve_session_socket

logger = structlog.get_logger()

router = APIRouter()


def get_ssh_manager() -> SSHSessionManager:
    return ssh_manager


Manager = Annotated[SSHSessionManager, Depends(get_ssh_manager)]
SessionId = Annotated[str, Path(pattern=SESSION_ID_PATTERN)]

CONNECT_ERRORS = {
    404: {"description": "Ключа key_id нет в хранилище"},
    409: {
        "description": (
            "Ключ сервера не подтверждён (ssh_host_key_unknown), изменился "
            "(ssh_host_key_mismatch) или отозван в known_hosts OpenSSH "
            "(ssh_host_key_revoked). В ответе host_key: {host, port, fingerprint, key_type, "
            "expected_fingerprint?, source?}. UI показывает отпечаток, после подтверждения "
            "пользователем сохраняет его (POST /api/ssh/known-hosts) и подключается снова. "
            "Ключ, который уже записан в known_hosts OpenSSH на этом компьютере, backend "
            "подтверждает сам"
        )
    },
    422: {"description": "Неверные параметры (например, нет файла ключа)"},
    429: {"description": "Открыто слишком много сессий (SSH_MAX_SESSIONS)"},
    502: {"description": "Воркер не смог подключиться: хост недоступен, ключ или пароль"},
    503: {"description": "Воркер не запущен или IPC недоступен"},
    504: {"description": "Сервер не ответил за SSH_CONNECT_TIMEOUT (ssh_connect_timeout)"},
}
EXEC_ERRORS = {
    404: {"description": "Сессии session_id нет"},
    409: {"description": "Нет открытой сессии, или воркер её уже не знает"},
    502: {"description": "Ошибка воркера"},
    503: {"description": "Воркер не запущен или IPC недоступен"},
    504: {"description": "Воркер не вернул результат даже после лимита команды"},
}
NOT_FOUND = {404: {"description": "Сессии session_id нет"}}


# ------------------------------------------------------------ /sessions


@router.get("/sessions", response_model=list[SSHSessionInfo], summary="Открытые SSH-сессии")
async def list_sessions(manager: Manager) -> list[SSHSessionInfo]:
    return [_info(session) for session in manager.sessions()]


@router.post(
    "/sessions",
    response_model=SSHSessionInfo,
    status_code=201,
    summary="Открыть SSH-сессию",
    responses=CONNECT_ERRORS,
)
async def create_session(
    request: SSHConnectRequest, manager: Manager, keys: Keys
) -> SSHSessionInfo:
    """Способ входа ровно один: `key_id` (ключ из хранилища), `key_path` (файл ключа, при
    необходимости с `passphrase`) или `password`. Уже открытые сессии продолжают работать.
    """
    return _info(await _open(request, manager, keys))


@router.get(
    "/sessions/{session_id}",
    response_model=SSHSessionInfo,
    summary="Одна SSH-сессия",
    responses=NOT_FOUND,
)
async def get_session(session_id: SessionId, manager: Manager) -> SSHSessionInfo:
    return _info(manager.get(session_id))


@router.delete(
    "/sessions/{session_id}",
    response_model=SSHDisconnectResponse,
    summary="Закрыть SSH-сессию",
    responses=NOT_FOUND,
)
async def delete_session(session_id: SessionId, manager: Manager) -> SSHDisconnectResponse:
    """Подключённые WebSocket получают `closed` и закрываются. Если в сессии ещё
    выполняется команда, она прерывается вместе с сессией."""
    session, message = await manager.close(session_id)
    return SSHDisconnectResponse(session_id=session.id, message=message)


@router.post(
    "/sessions/{session_id}/exec",
    response_model=SSHExecResponse,
    summary="Выполнить команду в сессии",
    responses=EXEC_ERRORS,
)
async def exec_in_session(
    session_id: SessionId, request: SSHCommandRequest, manager: Manager
) -> SSHExecResponse:
    """Команды одной сессии выполняются по очереди; результат попадает и в ответ,
    и в буфер вывода сессии (его видят подключённые WebSocket)."""
    return _exec_response(await _execute(manager, session_id, request))


@router.get(
    "/sessions/{session_id}/output",
    response_model=SSHOutputResponse,
    summary="Буфер вывода сессии",
    responses=NOT_FOUND,
)
async def session_output(
    session_id: SessionId,
    manager: Manager,
    since: Annotated[int, Query(ge=0, description="Вернуть записи с seq больше этого")] = 0,
    limit: Annotated[int | None, Query(ge=1, le=10_000)] = None,
) -> SSHOutputResponse:
    session = manager.get(session_id)
    replay = session.buffer.since(since, limit)
    return SSHOutputResponse(
        session_id=session.id,
        entries=[entry.as_dict() for entry in replay.entries],
        gap=replay.gap,
        first_seq=replay.first_seq,
        last_seq=replay.last_seq,
    )


@router.websocket("/sessions/{session_id}/ws")
async def session_websocket(
    websocket: WebSocket,
    session_id: str,
    manager: Manager,
    since: Annotated[int, Query(ge=0)] = 0,
) -> None:
    """Терминал сессии: живой вывод, команды из UI. Протокол в app/plugins/ssh/ws.py."""
    await serve_session_socket(websocket, manager, session_id, since)


# ----------------------------------------- одна «текущая» сессия (этап 1)


@router.post(
    "/connect",
    response_model=SSHConnectResponse,
    status_code=201,
    summary="Открыть SSH-сессию (то же, что POST /sessions)",
    responses=CONNECT_ERRORS,
)
async def connect(request: SSHConnectRequest, manager: Manager, keys: Keys) -> SSHConnectResponse:
    session = await _open(request, manager, keys)
    return SSHConnectResponse(
        session_id=session.id,
        message="Connected successfully",
        host=session.host,
        port=session.port,
        username=session.username,
        auth=session.auth,
        key_id=session.key_id,
    )


@router.post(
    "/exec",
    response_model=SSHExecResponse,
    summary="Выполнить команду (без session_id в последней открытой сессии)",
    responses=EXEC_ERRORS,
)
async def exec_command(request: SSHExecRequest, manager: Manager) -> SSHExecResponse:
    """Лимит `timeout` применяется на сервере (coreutils `timeout`): команда получает TERM,
    через 3 секунды KILL, а ответ приходит с `timed_out=true`, кодом 124/137 и тем
    выводом, что успел накопиться. 504 значит, что воркер не ответил вовсе.
    """
    return _exec_response(await _execute(manager, request.session_id, request))


@router.post(
    "/disconnect",
    response_model=SSHDisconnectResponse,
    summary="Закрыть SSH-сессию (без session_id последнюю открытую)",
    responses={**NOT_FOUND, 409: {"description": "Нет открытой сессии"}},
)
async def disconnect(
    manager: Manager, request: SSHDisconnectRequest | None = None
) -> SSHDisconnectResponse:
    session_id = request.session_id if request is not None else None
    session, message = await manager.close(session_id)
    return SSHDisconnectResponse(session_id=session.id, message=message)


@router.get(
    "/status",
    response_model=SSHStatusResponse,
    summary="Состояние SSH-сессии",
    responses=NOT_FOUND,
)
async def status(
    manager: Manager,
    session_id: Annotated[str | None, Query(pattern=SESSION_ID_PATTERN)] = None,
) -> SSHStatusResponse:
    """Без session_id показывает текущую (последнюю открытую) сессию."""
    session = manager.get(session_id) if session_id else manager.current
    if session is None:
        return SSHStatusResponse(connected=False)
    info = session.info()
    fields = {name: info[name] for name in SSHStatusResponse.model_fields if name in info}
    return SSHStatusResponse(connected=True, sessions=len(manager), **fields)


# ------------------------------------------------- доверенные ключи (TOFU)


@router.get(
    "/known-hosts",
    response_model=list[KnownHostInfo],
    summary="Доверенные ключи серверов",
)
async def list_known_hosts() -> list[KnownHostInfo]:
    """Отпечатки ключей серверов по host:port: которые подтвердил пользователь (source
    `user`) и которые backend нашёл в known_hosts OpenSSH на этом компьютере (`openssh`)."""
    return [_known(host) for host in await get_known_hosts().list_hosts()]


@router.post(
    "/known-hosts",
    response_model=KnownHostChange,
    status_code=201,
    summary="Доверять ключу сервера",
    responses={
        409: {
            "description": (
                "Для сервера уже сохранён другой отпечаток (ssh_known_host_conflict). "
                "Заменить его можно только с replace=true по явному действию пользователя"
            )
        }
    },
)
async def trust_host_key(request: KnownHostRequest) -> KnownHostChange:
    """Вызывать только после того, как пользователь увидел отпечаток (`host_key` в ответе
    409 на подключение) и подтвердил его. Затем подключение повторяется."""
    record, changed = await get_known_hosts().trust(
        request.host,
        request.port,
        request.fingerprint,
        request.key_type,
        replace=request.replace,
    )
    return KnownHostChange(changed=changed, known_host=_known(record))


@router.delete(
    "/known-hosts",
    response_model=KnownHostInfo,
    summary="Забыть ключ сервера",
    responses={404: {"description": "Для сервера нет сохранённого ключа"}},
)
async def forget_host_key(
    host: Annotated[str, Query(min_length=1, max_length=255)],
    port: Annotated[int, Query(ge=1, le=65535)] = 22,
) -> KnownHostInfo:
    """Следующее подключение к серверу снова попросит подтвердить его ключ."""
    return _known(await get_known_hosts().remove(host, port))


# --------------------------------------------------------------- helpers


def _known(record: KnownHost) -> KnownHostInfo:
    return KnownHostInfo(**record.model_dump())


async def _open(request: SSHConnectRequest, manager: SSHSessionManager, keys: Keys) -> SSHSession:
    params = ConnectParams(
        host=request.host,
        port=request.port,
        username=request.username,
        key_id=request.key_id,
        key_path=request.key_path,
        passphrase=_secret(request.passphrase),
        password=_secret(request.password),
    )
    logger.info(
        "SSH connect",
        host=params.host,
        port=params.port,
        username=params.username,
        auth=params.auth,
        key_id=params.key_id,
    )
    return await manager.open(params, keys)


async def _execute(
    manager: SSHSessionManager, session_id: str | None, request: SSHCommandRequest
) -> ExecResult:
    return await manager.execute(
        session_id, request.command, timeout=request.timeout, track_cwd=request.track_cwd
    )


def _exec_response(result: ExecResult) -> SSHExecResponse:
    return SSHExecResponse(
        session_id=result.session_id,
        seq=result.seq,
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        cwd=result.cwd,
        timed_out=result.timed_out,
        duration_ms=result.duration_ms,
    )


def _info(session: SSHSession) -> SSHSessionInfo:
    return SSHSessionInfo(**session.info())


def _secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None
