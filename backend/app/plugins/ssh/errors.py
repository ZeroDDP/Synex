from __future__ import annotations

from typing import Any

from app.core.errors import AppError


class SSHConnectError(AppError):
    """Воркер жив, но SSH не поднялся: хост недоступен, ключ или пароль не подошли."""

    code = "ssh_connect_failed"
    status_code = 502


class SSHConnectTimeoutError(AppError):
    """Сервер не ответил за SSH_CONNECT_TIMEOUT: неверный адрес, файрвол или он выключен."""

    code = "ssh_connect_timeout"
    status_code = 504


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


class SSHSessionBusyError(AppError):
    """Служебная команда плагина не дождалась, пока сессия освободится."""

    code = "ssh_session_busy"
    status_code = 409


class SSHInteractiveCommandError(AppError):
    """Программе нужен терминал (vim, top, tmux), а команда идёт без него: висела бы до
    таймаута. В extra: program и hint (чем заменить). allow_interactive=true отправит
    всё равно."""

    code = "ssh_interactive_command"
    status_code = 422


class SSHInterruptUnavailableError(AppError):
    """Команду нельзя прервать: воркер не даёт второе подключение к серверу (через него
    идёт сигнал), или команда ушла без обёртки (track_cwd=false)."""

    code = "ssh_interrupt_unavailable"
    status_code = 409


class SSHHostKeyUnknownError(AppError):
    """Ключ сервера ещё не подтверждён (TOFU): UI показывает отпечаток пользователю,
    сохраняет его (POST /api/ssh/known-hosts) и подключается снова."""

    code = "ssh_host_key_unknown"
    status_code = 409

    def __init__(self, detail: str, host_key: dict[str, Any]) -> None:
        super().__init__(detail)
        self.host_key = host_key
        self.extra = {"host_key": host_key}


class SSHHostKeyMismatchError(AppError):
    """Ключ сервера не совпал с подтверждённым: подключение не продолжается и само не
    повторяется. Сохранённый отпечаток меняет только пользователь (replace=true)."""

    code = "ssh_host_key_mismatch"
    status_code = 409

    def __init__(self, detail: str, host_key: dict[str, Any]) -> None:
        super().__init__(detail)
        self.host_key = host_key
        self.extra = {"host_key": host_key}


class SSHHostKeyRevokedError(AppError):
    """Ключ сервера помечен как отозванный (@revoked) в known_hosts OpenSSH на этом
    компьютере: подключения нет."""

    code = "ssh_host_key_revoked"
    status_code = 409

    def __init__(self, detail: str, host_key: dict[str, Any]) -> None:
        super().__init__(detail)
        self.host_key = host_key
        self.extra = {"host_key": host_key}
