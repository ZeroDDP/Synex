"""Зашифрованное хранилище SSH-ключей в локальном JSON-файле.

Формат keys.json:

    {
      "version": 1,
      "key_check": "<Fernet-токен>",       # проверка, что мастер-ключ тот же
      "keys": [
        {"id", "name", "key_type", "bits", "fingerprint", "public_key", "comment",
         "created_at", "updated_at",
         "private_key": "<Fernet-токен>"}  # приватный ключ, OpenSSH-формат
      ]
    }

В открытом виде в файле только метаданные и публичные ключи. Запись атомарная
(временный файл + os.replace), на Linux/macOS права 0600 и папка 0700. На Windows
файл лежит в %LOCALAPPDATA% и наследует права профиля пользователя.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import tempfile
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path

import structlog
from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, Field, ValidationError

from app.core.locks import LoopLock
from app.keystore.errors import (
    KeyExistsError,
    KeyNotFoundError,
    KeystoreCorruptedError,
    KeystoreUnavailableError,
)
from app.keystore.master_key import load_master_key
from app.keystore.ssh_keys import (
    GeneratedKeyType,
    ParsedKey,
    generate_private_key,
    parse_private_key,
)

logger = structlog.get_logger()

FILE_VERSION = 1
KEY_CHECK_PLAINTEXT = b"synex-keystore-v1"
MasterKeyLoader = Callable[..., bytes]


class KeyRecord(BaseModel):
    id: str
    name: str
    key_type: str
    bits: int
    fingerprint: str
    public_key: str
    comment: str = ""
    created_at: datetime
    updated_at: datetime
    private_key: str = Field(repr=False)  # Fernet-токен

    @property
    def public_key_line(self) -> str:
        """Строка для authorized_keys: ключ и комментарий."""
        return f"{self.public_key} {self.comment}".strip()


class KeystoreFile(BaseModel):
    version: int = FILE_VERSION
    key_check: str
    keys: list[KeyRecord] = Field(default_factory=list)


class KeyStore:
    def __init__(self, path: Path, master_key_loader: MasterKeyLoader = load_master_key) -> None:
        self.path = path
        self._load_master_key = master_key_loader
        self._fernet: Fernet | None = None
        self._lock = LoopLock()

    @property
    def runtime_dir(self) -> Path:
        """Папка для временных файлов ключей, которые читает воркер."""
        return self.path.parent / "runtime"

    # ------------------------------------------------------------------ read

    async def list_keys(self) -> list[KeyRecord]:
        async with self._lock:
            content = self._read()
        return list(content.keys) if content else []

    async def get_key(self, key_id: str) -> KeyRecord:
        async with self._lock:
            return self._find(self._read(), key_id)

    async def decrypt_private_key(self, key_id: str) -> bytes:
        async with self._lock:
            content = self._read()
            record = self._find(content, key_id)
            fernet = await self._fernet_for(content)
        try:
            return fernet.decrypt(record.private_key.encode())
        except InvalidToken as e:
            raise KeystoreCorruptedError(f"Key '{record.name}' cannot be decrypted") from e

    # ----------------------------------------------------------------- write

    async def import_key(
        self, name: str, private_key: bytes, passphrase: bytes | None = None, comment: str = ""
    ) -> KeyRecord:
        # bcrypt KDF у ключей с парольной фразой работает заметное время
        parsed = await asyncio.to_thread(parse_private_key, private_key, passphrase)
        return await self._add(name, parsed, comment)

    async def generate_key(
        self,
        name: str,
        key_type: GeneratedKeyType = "ed25519",
        bits: int | None = None,
        comment: str = "",
    ) -> KeyRecord:
        parsed = await asyncio.to_thread(generate_private_key, key_type, bits)
        return await self._add(name, parsed, comment)

    async def update_key(
        self, key_id: str, *, name: str | None = None, comment: str | None = None
    ) -> KeyRecord:
        async with self._lock:
            content = self._read()
            record = self._find(content, key_id)
            assert content is not None
            if name is not None and name != record.name:
                self._ensure_unique_name(content, name, exclude_id=record.id)
                record.name = name
            if comment is not None:
                record.comment = comment
            record.updated_at = _now()
            self._write(content)
        logger.info("SSH key updated", key_id=key_id)
        return record

    async def delete_key(self, key_id: str) -> None:
        async with self._lock:
            content = self._read()
            record = self._find(content, key_id)
            assert content is not None
            content.keys.remove(record)
            self._write(content)
        logger.info("SSH key deleted", key_id=key_id, fingerprint=record.fingerprint)

    async def _add(self, name: str, parsed: ParsedKey, comment: str) -> KeyRecord:
        async with self._lock:
            content = self._read()
            fernet = await self._fernet_for(content)
            if content is None:
                content = KeystoreFile(key_check=fernet.encrypt(KEY_CHECK_PLAINTEXT).decode())

            self._ensure_unique_name(content, name)
            for existing in content.keys:
                if existing.fingerprint == parsed.fingerprint:
                    raise KeyExistsError(f"This key is already stored as '{existing.name}'")

            now = _now()
            record = KeyRecord(
                id=uuid.uuid4().hex,
                name=name,
                key_type=parsed.key_type,
                bits=parsed.bits,
                fingerprint=parsed.fingerprint,
                public_key=parsed.public_key,
                comment=comment,
                created_at=now,
                updated_at=now,
                private_key=fernet.encrypt(parsed.private_key).decode(),
            )
            content.keys.append(record)
            self._write(content)
        logger.info("SSH key stored", key_id=record.id, fingerprint=record.fingerprint)
        return record

    # ------------------------------------------------- temporary file for worker

    @contextlib.asynccontextmanager
    async def temporary_key_file(self, key_id: str) -> AsyncIterator[Path]:
        """Расшифровывает ключ во временный файл на время ssh_connect.

        synex_worker принимает только путь к ключу (CoreDocs 5.1), поэтому без файла не
        обойтись. Файл создаётся с правами 0600 в папке хранилища и удаляется сразу
        после подключения. Остатки после аварийного завершения чистит cleanup_runtime().
        """
        secret = await self.decrypt_private_key(key_id)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        _restrict_dir(self.runtime_dir)
        fd, name = tempfile.mkstemp(prefix="key_", dir=self.runtime_dir)
        path = Path(name)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(secret)
            yield path
        finally:
            await _remove(path)

    def cleanup_runtime(self) -> None:
        if not self.runtime_dir.is_dir():
            return
        for leftover in self.runtime_dir.iterdir():
            with contextlib.suppress(OSError):
                leftover.unlink()
                logger.info("Removed leftover temporary key file", path=str(leftover))

    # ------------------------------------------------------------- internals

    async def _fernet_for(self, content: KeystoreFile | None) -> Fernet:
        if self._fernet is not None:
            return self._fernet
        key = await asyncio.to_thread(self._load_master_key, create=content is None)
        try:
            fernet = Fernet(key)
        except (ValueError, TypeError) as e:
            raise KeystoreUnavailableError("Master key is not a valid Fernet key") from e
        if content is not None:
            try:
                fernet.decrypt(content.key_check.encode())
            except InvalidToken as e:
                raise KeystoreCorruptedError(
                    f"Master key does not match {self.path}: the file was encrypted with "
                    "another key (different user, machine or KEYSTORE_MASTER_KEY)"
                ) from e
        self._fernet = fernet
        return fernet

    def _read(self) -> KeystoreFile | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as e:
            raise KeystoreCorruptedError(f"Cannot read {self.path}: {e}") from e
        try:
            content = KeystoreFile.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError) as e:
            raise KeystoreCorruptedError(f"{self.path} is damaged: {e}") from e
        if content.version != FILE_VERSION:
            raise KeystoreCorruptedError(f"Unsupported keystore version {content.version}")
        return content

    def _write(self, content: KeystoreFile) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _restrict_dir(self.path.parent)
        tmp = self.path.with_name(f"{self.path.name}.tmp")
        data = content.model_dump_json(indent=2)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, self.path)

    @staticmethod
    def _find(content: KeystoreFile | None, key_id: str) -> KeyRecord:
        for record in content.keys if content else []:
            if record.id == key_id:
                return record
        raise KeyNotFoundError(f"SSH key '{key_id}' not found")

    @staticmethod
    def _ensure_unique_name(
        content: KeystoreFile, name: str, exclude_id: str | None = None
    ) -> None:
        for record in content.keys:
            if record.id != exclude_id and record.name.casefold() == name.casefold():
                raise KeyExistsError(f"Key named '{name}' already exists")


def _now() -> datetime:
    return datetime.now(UTC)


def _restrict_dir(path: Path) -> None:
    if sys.platform != "win32":
        with contextlib.suppress(OSError):
            path.chmod(0o700)


async def _remove(path: Path) -> None:
    # На Windows файл может быть ещё открыт воркером: несколько попыток
    for _ in range(20):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            await asyncio.sleep(0.1)
    logger.warning("Could not remove temporary key file, will retry on restart", path=str(path))
