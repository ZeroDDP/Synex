from __future__ import annotations

from app.core.errors import AppError


class LogsServerNotConnectedError(AppError):
    """К серверу нет открытой SSH-сессии (или нет сессии session_id у этого сервера)."""

    code = "logs_server_not_connected"
    status_code = 404


class LogsJournalUnavailableError(AppError):
    """На сервере нет journalctl (не systemd)."""

    code = "logs_journal_unavailable"
    status_code = 404


class LogsBadFilterError(AppError):
    """journalctl не принял фильтр: время, уровень, курсор или шаблон."""

    code = "logs_bad_filter"
    status_code = 422


class LogFileNotFoundError(AppError):
    code = "log_file_not_found"
    status_code = 404


class LogFileDeniedError(AppError):
    """Пользователь SSH не может читать файл (например, нет в группе adm)."""

    code = "log_file_permission_denied"
    status_code = 403


class LogFileIsDirectoryError(AppError):
    code = "log_file_is_directory"
    status_code = 422


class LogsTimeoutError(AppError):
    code = "logs_timeout"
    status_code = 504


class LogsCommandError(AppError):
    """journalctl или чтение файла завершились с ошибкой; текст ошибки в detail."""

    code = "logs_command_failed"
    status_code = 502
