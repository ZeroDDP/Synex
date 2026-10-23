from __future__ import annotations

import contextlib
import math
import time
from typing import Any

import structlog
from fastapi import APIRouter

from app.api.deps import Keys, Worker
from app.core.config import settings
from app.core.errors import AppError, WorkerCommandError, WorkerTimeoutError
from app.plugins.ssh.schemas import (
    SSHConnectRequest,
    SSHConnectResponse,
    SSHExecRequest,
    SSHExecResponse,
    SSHStatusResponse,
)
from app.plugins.ssh.session import ssh_state
from app.plugins.ssh.wrapper import KILL_AFTER, build_command, parse_output

logger = structlog.get_logger()

router = APIRouter()

# Текст ошибки воркера, когда ssh_exec пришёл раньше ssh_connect (CoreDocs, 7)
WORKER_NOT_CONNECTED = "Not connected"


class SSHConnectError(AppError):
    """Воркер жив, но SSH не поднялся: хост недоступен, ключ не подошёл и т.п."""

    code = "ssh_connect_failed"
    status_code = 502


class SSHNotConnectedError(AppError):
    code = "ssh_not_connected"
    status_code = 409


class SSHExecTimeoutError(AppError):
    """Воркер не вернул результат даже после лимита команды и запаса времени."""

    code = "ssh_exec_timeout"
    status_code = 504


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
    logger.info(
        "SSH connect",
        host=request.host,
        port=request.port,
        username=request.username,
        key_id=request.key_id,
    )
    async with ssh_state.lock:
        async with contextlib.AsyncExitStack() as stack:
            if request.key_id is not None:
                # Воркер принимает только путь: ключ из хранилища кладётся во временный
                # файл и удаляется сразу после ответа воркера.
                key_path = str(
                    await stack.enter_async_context(keys.temporary_key_file(request.key_id))
                )
            else:
                assert request.key_path is not None
                key_path = request.key_path

            payload = {
                "connection": {
                    "host": request.host,
                    "port": request.port,
                    "username": request.username,
                    "key_path": key_path,
                }
            }
            try:
                response = await worker.request("ssh_connect", payload)
            except WorkerCommandError as e:
                ssh_state.mark_disconnected()
                raise SSHConnectError(e.stderr) from e
            except Exception:
                # Воркер недоступен (503) или не ответил (504): JSON соберёт обработчик AppError
                ssh_state.mark_disconnected()
                raise

        ssh_state.mark_connected(
            request.host, username=request.username, port=request.port, key_id=request.key_id
        )

    return SSHConnectResponse(
        message=str(response.get("stdout") or "Connected successfully").strip(),
        host=request.host,
        port=request.port,
        username=request.username,
        key_id=request.key_id,
    )


@router.post(
    "/exec",
    response_model=SSHExecResponse,
    summary="Выполнить команду в открытой SSH-сессии",
    responses={
        409: {"description": "SSH-сессия не открыта"},
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
    limit = request.timeout or settings.SSH_EXEC_TIMEOUT

    async with ssh_state.lock:
        if not ssh_state.is_connected:
            raise SSHNotConnectedError("SSH session is not open. Call /api/ssh/connect first.")

        marker = ssh_state.marker
        if request.track_cwd:
            remote_limit = max(1, math.ceil(limit))
            command = build_command(request.command, ssh_state.cwd, marker, remote_limit)
            wait = remote_limit + KILL_AFTER + settings.SSH_EXEC_TIMEOUT_GRACE
        else:
            command = request.command
            wait = limit

        # Текст команды может содержать пароли и токены: только в DEBUG
        logger.debug("SSH exec", command=request.command, cwd=ssh_state.cwd, timeout=limit)
        started = time.monotonic()
        try:
            response = await worker.request("ssh_exec", {"command": command}, timeout=wait)
        except WorkerCommandError as e:
            if WORKER_NOT_CONNECTED in e.stderr:
                # Сессии в воркере нет (например, его перезапустили): синхронизируемся.
                ssh_state.mark_disconnected()
                raise SSHNotConnectedError(e.stderr) from e
            raise
        except WorkerTimeoutError as e:
            # Сессия в воркере может быть жива, состояние не трогаем.
            hint = (
                "The server-side limit did not stop it: the server may lack coreutils `timeout`"
                if request.track_cwd
                else "Without track_cwd the command is not limited on the server"
            )
            logger.warning("SSH exec timed out without a reply", waited=wait)
            raise SSHExecTimeoutError(
                f"No result from the worker within {wait:g}s. {hint}; the command may still be "
                "running and keep the worker busy."
            ) from e
        duration_ms = round((time.monotonic() - started) * 1000)

        stdout = str(response.get("stdout") or "")
        exit_code = _as_int(response.get("exit_code"), default=-1)
        timed_out = False
        if request.track_cwd:
            result = parse_output(stdout, marker, timeout_applied=True)
            stdout = result.stdout
            if result.exit_code is not None:
                # Воркер сейчас всегда отдаёт 0; код из shell точнее.
                exit_code = result.exit_code
            if result.cwd and ssh_state.marker == marker:
                ssh_state.cwd = result.cwd
            timed_out = result.timed_out

    if timed_out:
        logger.info("SSH exec stopped by timeout", timeout=limit, duration_ms=duration_ms)
    return SSHExecResponse(
        stdout=stdout.rstrip("\r\n"),
        stderr=str(response.get("stderr") or "").rstrip("\r\n"),
        exit_code=exit_code,
        cwd=ssh_state.cwd,
        timed_out=timed_out,
        duration_ms=duration_ms,
    )


@router.get("/status", response_model=SSHStatusResponse, summary="Состояние SSH-сессии")
async def status() -> SSHStatusResponse:
    return SSHStatusResponse(**ssh_state.snapshot())


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
