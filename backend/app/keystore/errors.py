from __future__ import annotations

from app.core.errors import AppError


class KeyNotFoundError(AppError):
    code = "key_not_found"
    status_code = 404


class KeyExistsError(AppError):
    code = "key_exists"
    status_code = 409


class InvalidKeyError(AppError):
    """Не удалось прочитать ключ: формат, неверная/отсутствующая парольная фраза, тип."""

    code = "invalid_key"
    status_code = 422


class KeystoreUnavailableError(AppError):
    """Нет мастер-ключа: системное хранилище недоступно или ключ задан неверно."""

    code = "keystore_unavailable"
    status_code = 503


class KeystoreCorruptedError(AppError):
    """Файл хранилища не читается или зашифрован другим мастер-ключом."""

    code = "keystore_corrupted"
    status_code = 500
