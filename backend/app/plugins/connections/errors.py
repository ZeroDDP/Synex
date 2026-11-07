from __future__ import annotations

from app.core.errors import AppError


class ConnectionNotFoundError(AppError):
    code = "connection_not_found"
    status_code = 404


class ConnectionExistsError(AppError):
    code = "connection_exists"
    status_code = 409


class InvalidConnectionError(AppError):
    """Способ входа не согласован: нет ключа, лишний пароль, файла ключа нет."""

    code = "invalid_connection"
    status_code = 422


class ConnectionSecretRequiredError(AppError):
    """Пароль (или фраза ключа) не сохранён: его нужно передать при подключении."""

    code = "connection_secret_required"
    status_code = 422


class ConnectionsCorruptedError(AppError):
    code = "connections_corrupted"
    status_code = 500
