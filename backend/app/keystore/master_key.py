"""Мастер-ключ Fernet, которым шифруются приватные SSH-ключи в keys.json.

Ключ лежит не рядом с файлом, а в системном хранилище учётных данных (через keyring):
Windows Credential Manager, macOS Keychain, Secret Service на Linux. Копия keys.json
без доступа к учётной записи пользователя бесполезна.

KEYSTORE_MASTER_KEY в .env/окружении заменяет системное хранилище там, где его нет.
"""

from __future__ import annotations

import structlog
from cryptography.fernet import Fernet

from app.core.config import settings
from app.keystore.errors import KeystoreCorruptedError, KeystoreUnavailableError

logger = structlog.get_logger()

KEYRING_SERVICE = "synex"
KEYRING_USERNAME = "keystore-master-key"


def load_master_key(*, create: bool) -> bytes:
    """Возвращает мастер-ключ.

    create=True: если ключа ещё нет, сгенерировать и сохранить в системное хранилище.
    create=False используется, когда keys.json уже существует: новый ключ его всё
    равно не расшифрует, поэтому лучше явная ошибка, чем тихая потеря ключей.
    """
    if settings.KEYSTORE_MASTER_KEY is not None:
        return _validated(settings.KEYSTORE_MASTER_KEY.get_secret_value(), "KEYSTORE_MASTER_KEY")

    try:
        import keyring

        stored = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
        if stored:
            return _validated(stored, "OS keychain")
        if not create:
            raise KeystoreCorruptedError(
                "Keystore file exists, but its master key is missing from the OS keychain "
                f"({KEYRING_SERVICE}/{KEYRING_USERNAME}). The stored keys cannot be decrypted."
            )
        key = Fernet.generate_key().decode()
        keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, key)
        logger.info(
            "Generated keystore master key and saved it to the OS keychain",
            backend=type(keyring.get_keyring()).__name__,
        )
        return key.encode()
    except (KeystoreCorruptedError, KeystoreUnavailableError):
        raise
    except Exception as e:
        raise KeystoreUnavailableError(
            f"OS keychain is not available ({e}). "
            "Set KEYSTORE_MASTER_KEY to a Fernet key to use the key storage without it."
        ) from e


def _validated(value: str, source: str) -> bytes:
    key = value.strip().encode()
    try:
        Fernet(key)
    except (ValueError, TypeError) as e:
        raise KeystoreUnavailableError(
            f"{source} is not a valid Fernet key (32 url-safe base64-encoded bytes)"
        ) from e
    return key
