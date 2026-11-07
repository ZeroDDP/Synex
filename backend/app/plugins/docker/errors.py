from __future__ import annotations

from app.core.errors import AppError


class DockerNotInstalledError(AppError):
    code = "docker_not_installed"
    status_code = 404


class DockerPermissionError(AppError):
    """Пользователь SSH не может обращаться к демону Docker (нет в группе docker)."""

    code = "docker_permission_denied"
    status_code = 403


class DockerDaemonUnavailableError(AppError):
    code = "docker_daemon_unavailable"
    status_code = 503


class DockerContainerNotFoundError(AppError):
    code = "docker_container_not_found"
    status_code = 404


class DockerContainerConflictError(AppError):
    """Действие не подходит к состоянию контейнера: удалить запущенный без force,
    поставить на паузу остановленный."""

    code = "docker_container_conflict"
    status_code = 409


class DockerTimeoutError(AppError):
    code = "docker_timeout"
    status_code = 504


class DockerCommandError(AppError):
    """docker завершился с ошибкой; текст ошибки docker в detail."""

    code = "docker_command_failed"
    status_code = 502
