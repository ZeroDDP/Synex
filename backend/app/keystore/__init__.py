"""Зашифрованное хранилище SSH-ключей (Fernet + JSON, мастер-ключ в OS keychain)."""

from __future__ import annotations

from app.core.config import settings
from app.keystore.errors import (
    InvalidKeyError,
    KeyExistsError,
    KeyNotFoundError,
    KeystoreCorruptedError,
    KeystoreUnavailableError,
)
from app.keystore.store import KeyRecord, KeyStore

__all__ = [
    "InvalidKeyError",
    "KeyExistsError",
    "KeyNotFoundError",
    "KeyRecord",
    "KeyStore",
    "KeystoreCorruptedError",
    "KeystoreUnavailableError",
    "get_keystore",
]

_keystore: KeyStore | None = None


def get_keystore() -> KeyStore:
    """Хранилище по пути из настроек (KEYSTORE_PATH или папка данных пользователя)."""
    global _keystore
    path = settings.keystore_path
    if _keystore is None or _keystore.path != path:
        _keystore = KeyStore(path)
    return _keystore

