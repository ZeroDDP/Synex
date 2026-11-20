from __future__ import annotations

from app.core.errors import AppError

# ----------------------------------------------------------------- на сервере


class SftpNotFoundError(AppError):
    code = "sftp_not_found"
    status_code = 404


class SftpPermissionError(AppError):
    code = "sftp_permission_denied"
    status_code = 403


class SftpProtectedPathError(AppError):
    """Корень и домашнюю папку удалять и перемещать нельзя."""

    code = "sftp_protected_path"
    status_code = 403


class SftpTargetExistsError(AppError):
    code = "sftp_target_exists"
    status_code = 409


class SftpIsADirectoryError(AppError):
    code = "sftp_is_a_directory"
    status_code = 409


class SftpNotADirectoryError(AppError):
    code = "sftp_not_a_directory"
    status_code = 409


class SftpNotAFileError(AppError):
    code = "sftp_not_a_file"
    status_code = 409


class SftpDirectoryNotEmptyError(AppError):
    code = "sftp_directory_not_empty"
    status_code = 409


class SftpBadPathError(AppError):
    code = "sftp_bad_path"
    status_code = 422


class SftpBadSearchError(AppError):
    """Поиску не сказали, что искать (нет ни name, ни contains), или в name есть "/"."""

    code = "sftp_bad_search"
    status_code = 422


class SftpCommandError(AppError):
    """Воркер или сервер не выполнил операцию; текст в detail."""

    code = "sftp_failed"
    status_code = 502
    reason = ""  # текст воркера для подстановки в сообщение («: Failure»), если есть


class SftpUnavailableError(AppError):
    """SSH-сервер не даёт SFTP: в его sshd_config нет строки Subsystem sftp."""

    code = "sftp_unavailable"
    status_code = 502


class SftpTimeoutError(AppError):
    code = "sftp_timeout"
    status_code = 504


# ------------------------------------------------------------------- передачи


class SftpTransferNotFoundError(AppError):
    code = "sftp_transfer_not_found"
    status_code = 404


class SftpTransferCorruptedError(AppError):
    """Размер или контрольная сумма после передачи не совпали: файл не сохранён."""

    code = "sftp_transfer_corrupted"
    status_code = 502


class SftpTransferCancelledError(AppError):
    code = "sftp_transfer_cancelled"
    status_code = 409


class SftpFolderTooLargeError(AppError):
    """В папке больше файлов и папок, чем SFTP_FOLDER_LIMIT."""

    code = "sftp_folder_too_large"
    status_code = 422


# --------------------------------------------------------- на этом компьютере


class SftpLocalNotFoundError(AppError):
    code = "sftp_local_not_found"
    status_code = 404


class SftpLocalNotADirectoryError(AppError):
    code = "sftp_local_not_a_directory"
    status_code = 409


class SftpLocalExistsError(AppError):
    code = "sftp_local_exists"
    status_code = 409


class SftpLocalPermissionError(AppError):
    code = "sftp_local_permission_denied"
    status_code = 403


class SftpLocalError(AppError):
    """Файл этого компьютера не прочитать или не записать (диск, имя, занят)."""

    code = "sftp_local_error"
    status_code = 422
