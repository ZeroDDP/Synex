from __future__ import annotations

from app.core.errors import AppError


class SSHConnectError(AppError):
    """Воркер жив, но SSH не поднялся: хост недоступен, ключ или пароль не подошли."""

    code = "ssh_connect_failed"
    status_code = 502


class SSHNotConnectedError(AppError):
    """Нет открытой сессии, или воркер её уже не знает (например, после перезапуска)."""

    code = "ssh_not_connected"
    status_code = 409


class SSHSessionNotFoundError(AppError):
    code = "ssh_session_not_found"
    status_code = 404


class SSHExecTimeoutError(AppError):
    """Воркер не вернул результат даже после лимита команды и запаса времени."""

    code = "ssh_exec_timeout"
    status_code = 504


class SSHTooManySessionsError(AppError):
    code = "ssh_too_many_sessions"
    status_code = 429
