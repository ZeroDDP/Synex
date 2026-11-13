"""Docker на сервере через SSH-сессию: команда docker CLI как служебная команда сессии
(ssh_manager.run_service: в терминал не попадает) и понятные ошибки по её выводу."""

from __future__ import annotations

import shlex
from typing import Literal

from app.core.config import settings
from app.core.errors import AppError
from app.plugins.docker.commands import VERSION_FORMAT, DockerVersion, parse_version
from app.plugins.docker.errors import (
    DockerCommandError,
    DockerContainerConflictError,
    DockerContainerNotFoundError,
    DockerDaemonUnavailableError,
    DockerNotInstalledError,
    DockerPermissionError,
    DockerTimeoutError,
)
from app.plugins.ssh.manager import ServiceResult, SSHSessionManager

DockerStatus = Literal["ok", "not_installed", "permission_denied", "daemon_unavailable"]

# Действие не подходит к состоянию контейнера (тексты демона Docker)
_CONFLICTS = (
    "is not running",
    "is already paused",
    "is not paused",
    "container is running",
    "is restarting",
)

_STATUS_OF_ERROR: dict[type[AppError], DockerStatus] = {
    DockerNotInstalledError: "not_installed",
    DockerPermissionError: "permission_denied",
    DockerDaemonUnavailableError: "daemon_unavailable",
}


async def run_docker(
    manager: SSHSessionManager,
    session_id: str,
    args: list[str],
    *,
    timeout: float | None = None,
    merge_stderr: bool = False,
    wait: float | None = None,
) -> ServiceResult:
    """`docker <args>` в сессии. Ошибку docker превращает в ошибку API (classify).
    wait: сколько ждать занятую сессию (по умолчанию SSH_SERVICE_WAIT)."""
    result = await _run(
        manager, session_id, args, timeout=timeout, merge_stderr=merge_stderr, wait=wait
    )
    error = classify(result)
    if error is not None:
        raise error
    return result


async def docker_version(
    manager: SSHSessionManager, session_id: str
) -> tuple[DockerStatus, str | None, DockerVersion]:
    """Есть ли Docker и доступен ли демон: (статус, текст ошибки, версии).

    Без исключений для «нет Docker», «нет прав» и «демон не запущен»: это ответ на
    вопрос, а не сбой. Версия клиента известна и без демона."""
    result = await _run(manager, session_id, ["version", "--format", VERSION_FORMAT])
    version = parse_version(result.stdout)
    error = classify(result)
    if error is None:
        return "ok", None, version
    status = _STATUS_OF_ERROR.get(type(error))
    if status is None:
        raise error
    return status, error.detail, version


def classify(result: ServiceResult) -> AppError | None:
    """Ошибка API по коду и тексту docker (тексты Docker 20.10 ... 29)."""
    if result.timed_out:
        return DockerTimeoutError(
            f"docker did not finish within the limit ({result.duration_ms} ms). "
            "The Docker daemon may be overloaded; DOCKER_TIMEOUT sets the limit"
        )
    if result.exit_code == 0:
        return None
    message = (result.stderr or result.stdout).strip() or f"docker exited with {result.exit_code}"
    lower = message.lower()
    if (
        result.exit_code == 127
        or "docker: not found" in lower
        or "docker: command not found" in lower
    ):
        return DockerNotInstalledError(f"Docker is not installed on the server: {message}")
    # Нет доступа к сокету демона (не «permission denied» внутри контейнера)
    socket_denied = "permission denied" in lower and any(
        place in lower for place in ("docker.sock", "docker daemon", "docker api")
    )
    if socket_denied or "a password is required" in lower:
        return DockerPermissionError(
            f"{message}. The SSH user cannot use Docker: add it to the docker group "
            "(sudo usermod -aG docker <user>) and reconnect, or set DOCKER_COMMAND"
        )
    if (
        "cannot connect to the docker daemon" in lower
        or "failed to connect to the docker api" in lower
        or "is the docker daemon running" in lower
    ):
        return DockerDaemonUnavailableError(f"The Docker daemon is not available: {message}")
    if "no such container" in lower:
        return DockerContainerNotFoundError(message)
    if any(state in lower for state in _CONFLICTS):
        return DockerContainerConflictError(message)
    return DockerCommandError(message)


async def _run(
    manager: SSHSessionManager,
    session_id: str,
    args: list[str],
    *,
    timeout: float | None = None,
    merge_stderr: bool = False,
    wait: float | None = None,
) -> ServiceResult:
    # DOCKER_COMMAND из настроек ("docker", "sudo -n docker") подставляется как есть
    command = " ".join([settings.DOCKER_COMMAND, *(shlex.quote(arg) for arg in args)])
    if merge_stderr:
        command += " 2>&1"
    return await manager.run_service(
        session_id, command, timeout=timeout or settings.DOCKER_TIMEOUT, wait=wait, name="docker"
    )
