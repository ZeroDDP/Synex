"""Docker Engine requests through the session's Rust worker."""

import json
from typing import Any

from app.core.config import settings
from app.core.errors import WorkerCommandError
from app.plugins.docker.errors import (
    DockerCommandError,
    DockerContainerConflictError,
    DockerContainerNotFoundError,
    DockerDaemonUnavailableError,
    DockerNotInstalledError,
    DockerPermissionError,
    DockerTimeoutError,
)
from app.plugins.ssh.manager import SSHSessionManager

_ERRORS = {
    error.code: error
    for error in (
        DockerCommandError,
        DockerContainerConflictError,
        DockerContainerNotFoundError,
        DockerDaemonUnavailableError,
        DockerNotInstalledError,
        DockerPermissionError,
        DockerTimeoutError,
    )
}


def docker_error(exc: WorkerCommandError):
    code, _, detail = exc.stderr.partition(":")
    return _ERRORS.get(code, DockerCommandError)(detail.strip() or exc.stderr)


async def docker_request(
    manager: SSHSessionManager,
    session_id: str,
    operation: str,
    payload: dict | None = None,
    *,
    timeout: float | None = None,
    wait: float | None = None,
) -> Any:
    try:
        reply = await manager.request(
            session_id,
            operation,
            payload,
            timeout=timeout or settings.DOCKER_TIMEOUT,
            wait=wait,
            name="docker",
        )
    except WorkerCommandError as exc:
        raise docker_error(exc) from exc
    if operation == "docker_container_remove":
        return None
    try:
        return json.loads(reply["stdout"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DockerCommandError(f"Invalid response from {operation}") from exc
