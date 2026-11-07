from __future__ import annotations

from app.core.errors import AppError


class GitNotInstalledError(AppError):
    code = "git_not_installed"
    status_code = 404


class GitPathNotFoundError(AppError):
    code = "git_path_not_found"
    status_code = 404


class GitNotARepositoryError(AppError):
    code = "git_not_a_repository"
    status_code = 404


class GitConflictError(AppError):
    """pull невозможен без ручного вмешательства: локальные изменения, расхождение с
    remote, нет upstream или detached HEAD. Код ошибки уточняет причину."""

    code = "git_conflict"
    status_code = 409

    def __init__(self, detail: str, code: str) -> None:
        super().__init__(detail)
        self.code = code


class GitRemoteError(AppError):
    """remote недоступен: сеть, адрес или нет доступа (ключ, логин)."""

    code = "git_remote_unavailable"
    status_code = 502


class GitTimeoutError(AppError):
    code = "git_timeout"
    status_code = 504


class GitCommandError(AppError):
    """git завершился с ошибкой; текст git в detail."""

    code = "git_command_failed"
    status_code = 502
