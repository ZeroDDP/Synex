from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Query

from app.api.deps import Keys, Worker
from app.plugins.ssh import service
from app.plugins.ssh.schemas import (
    SESSION_ID_PATTERN,
    SSHConnectRequest,
    SSHConnectResponse,
    SSHDisconnectRequest,
    SSHDisconnectResponse,
    SSHExecRequest,
    SSHExecResponse,
    SSHStatusResponse,
)
from app.plugins.ssh.session import ssh_sessions

logger = structlog.get_logger()

router = APIRouter()


@router.post(
    "/connect",
    response_model=SSHConnectResponse,
    status_code=201,
    summary="Открыть SSH-сессию через synex_worker",
    responses={
        404: {"description": "Ключа key_id нет в хранилище"},
        422: {"description": "Неверные параметры (например, нет файла ключа)"},
        502: {"description": "Воркер не смог подключиться: хост недоступен, ключ не подошёл"},
        503: {"description": "Воркер не запущен или IPC недоступен"},
        504: {"description": "Воркер не ответил вовремя"},
    },
)
async def connect(request: SSHConnectRequest, worker: Worker, keys: Keys) -> SSHConnectResponse:
    """Каждый вызов открывает новую сессию, уже открытые продолжают работать.

    Способ входа ровно один: `key_id` (ключ из хранилища), `key_path` (файл ключа, при
    необходимости с `passphrase`) или `password`.
    """
    params = service.ConnectParams(
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
    session = await service.connect(worker, keys, params)
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
    summary="Выполнить команду в открытой SSH-сессии",
    responses={
        404: {"description": "Сессии session_id нет"},
        409: {"description": "Нет открытой сессии, или воркер её уже не знает"},
        502: {"description": "Ошибка воркера"},
        503: {"description": "Воркер не запущен или IPC недоступен"},
        504: {"description": "Воркер не вернул результат даже после лимита команды"},
    },
)
async def exec_command(request: SSHExecRequest, worker: Worker) -> SSHExecResponse:
    """Команды выполняются по одной: следующая ждёт, пока закончится предыдущая.

    Лимит `timeout` применяется на сервере (coreutils `timeout`): команда получает TERM,
    через 3 секунды KILL, а ответ приходит с `timed_out=true`, кодом 124/137 и тем
    выводом, что успел накопиться. 504 значит, что воркер не ответил вовсе.
    """
    result = await service.execute(
        worker,
        request.session_id,
        request.command,
        timeout=request.timeout,
        track_cwd=request.track_cwd,
    )
    return SSHExecResponse(
        session_id=result.session_id,
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        cwd=result.cwd,
        timed_out=result.timed_out,
        duration_ms=result.duration_ms,
    )


@router.post(
    "/disconnect",
    response_model=SSHDisconnectResponse,
    summary="Закрыть SSH-сессию",
    responses={
        404: {"description": "Сессии session_id нет"},
        409: {"description": "Нет открытой сессии"},
    },
)
async def disconnect(
    worker: Worker, request: SSHDisconnectRequest | None = None
) -> SSHDisconnectResponse:
    session_id = request.session_id if request is not None else None
    session, message = await service.disconnect(worker, session_id)
    return SSHDisconnectResponse(session_id=session.id, message=message)


@router.get(
    "/status",
    response_model=SSHStatusResponse,
    summary="Состояние SSH-сессии",
    responses={404: {"description": "Сессии session_id нет"}},
)
async def status(
    session_id: Annotated[str | None, Query(pattern=SESSION_ID_PATTERN)] = None,
) -> SSHStatusResponse:
    """Без session_id показывает текущую (последнюю открытую) сессию."""
    session = ssh_sessions.get(session_id) if session_id else ssh_sessions.current
    if session is None:
        return SSHStatusResponse(connected=False)
    return SSHStatusResponse(connected=True, sessions=len(ssh_sessions), **session.snapshot())


def _secret(value) -> str | None:
    return value.get_secret_value() if value is not None else None
