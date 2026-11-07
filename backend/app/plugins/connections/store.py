"""Сохранённые подключения к серверам: connections.json в папке данных пользователя.

Формат:

    {
      "version": 1,
      "connections": [
        {"id", "name", "host", "port", "username", "group",
         "auth": "key_id" | "key_path" | "password",
         "key_id", "key_path",
         "password": "<Fernet-токен>" | null,     # пароль, если его сохранили
         "passphrase": "<Fernet-токен>" | null,   # фраза ключа из key_path
         "created_at", "updated_at", "last_connected_at"}
      ]
    }

Пароли и фразы шифруются мастер-ключом хранилища ключей (KeyStore.encrypt_secret),
остальное лежит открыто. Пароль можно не сохранять: тогда его передают при каждом
подключении. Запись атомарная, права только для пользователя (app/core/files.py).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

import structlog
from pydantic import BaseModel, Field, ValidationError

from app.core.config import settings
from app.core.files import write_private_file
from app.core.locks import LoopLock
from app.keystore import KeyStore, get_keystore
from app.plugins.connections.errors import (
    ConnectionExistsError,
    ConnectionNotFoundError,
    ConnectionsCorruptedError,
    InvalidConnectionError,
)

logger = structlog.get_logger()

FILE_VERSION = 1
AuthMethod = Literal["key_id", "key_path", "password"]


class _Keep:
    def __repr__(self) -> str:
        return "KEEP"


KEEP: Final = _Keep()  # оставить сохранённый секрет как есть (при изменении)


class ConnectionRecord(BaseModel):
    id: str
    name: str
    host: str
    port: int = 22
    username: str
    group: str | None = None
    auth: AuthMethod
    key_id: str | None = None
    key_path: str | None = None
    password: str | None = Field(default=None, repr=False)  # Fernet-токен
    passphrase: str | None = Field(default=None, repr=False)  # Fernet-токен
    created_at: datetime
    updated_at: datetime
    last_connected_at: datetime | None = None


class ConnectionsFile(BaseModel):
    version: int = FILE_VERSION
    connections: list[ConnectionRecord] = Field(default_factory=list)


@dataclass(frozen=True)
class ConnectionAuth:
    """Способ входа в открытом виде. password/passphrase: строка, None (не сохранять)
    или KEEP (оставить сохранённый)."""

    method: AuthMethod
    key_id: str | None = None
    key_path: str | None = None
    password: str | _Keep | None = None
    passphrase: str | _Keep | None = None


class ConnectionStore:
    def __init__(self, path: Path, keys: KeyStore) -> None:
        self.path = path
        self.keys = keys
        self._lock = LoopLock()

    # ------------------------------------------------------------------ read

    async def list_connections(self) -> list[ConnectionRecord]:
        async with self._lock:
            return list(self._read().connections)

    async def get(self, connection_id: str) -> ConnectionRecord:
        async with self._lock:
            return self._find(self._read(), connection_id)

    async def secrets(self, record: ConnectionRecord) -> tuple[str | None, str | None]:
        """Расшифрованные (пароль, фраза ключа); None, если не сохранены."""
        password = await self.keys.decrypt_secret(record.password) if record.password else None
        phrase = await self.keys.decrypt_secret(record.passphrase) if record.passphrase else None
        return password, phrase

    # ----------------------------------------------------------------- write

    async def create(
        self,
        *,
        name: str,
        host: str,
        username: str,
        auth: ConnectionAuth,
        port: int = 22,
        group: str | None = None,
    ) -> ConnectionRecord:
        check_auth(auth)
        tokens = await self._tokens(auth, None)
        async with self._lock:
            content = self._read()
            self._ensure_unique_name(content, name)
            now = _now()
            record = ConnectionRecord(
                id=uuid.uuid4().hex,
                name=name,
                host=host,
                port=port,
                username=username,
                group=group,
                auth=auth.method,
                key_id=auth.key_id,
                key_path=auth.key_path,
                created_at=now,
                updated_at=now,
                **tokens,
            )
            content.connections.append(record)
            self._write(content)
        logger.info("Connection saved", connection_id=record.id, host=host, auth=auth.method)
        return record

    async def update(
        self, connection_id: str, *, auth: ConnectionAuth | None = None, **fields: Any
    ) -> ConnectionRecord:
        """fields: name, host, port, username, group. auth: новый способ входа целиком
        (секреты, которые не меняются, передаются как KEEP)."""
        unknown = set(fields) - {"name", "host", "port", "username", "group"}
        if unknown:
            raise TypeError(f"unknown connection fields: {', '.join(sorted(unknown))}")
        current = await self.get(connection_id)
        tokens: dict[str, str | None] = {}
        if auth is not None:
            check_auth(auth, current)
            tokens = await self._tokens(auth, current)
        async with self._lock:
            content = self._read()
            record = self._find(content, connection_id)
            if "name" in fields and fields["name"] != record.name:
                self._ensure_unique_name(content, fields["name"], exclude_id=record.id)
            for field, value in fields.items():
                setattr(record, field, value)
            if auth is not None:
                record.auth = auth.method
                record.key_id = auth.key_id
                record.key_path = auth.key_path
                record.password = tokens["password"]
                record.passphrase = tokens["passphrase"]
            record.updated_at = _now()
            self._write(content)
        logger.info("Connection updated", connection_id=connection_id)
        return record

    async def delete(self, connection_id: str) -> None:
        async with self._lock:
            content = self._read()
            content.connections.remove(self._find(content, connection_id))
            self._write(content)
        logger.info("Connection deleted", connection_id=connection_id)

    async def mark_connected(self, connection_id: str) -> None:
        async with self._lock:
            content = self._read()
            self._find(content, connection_id).last_connected_at = _now()
            self._write(content)

    # ------------------------------------------------------------- internals

    async def _tokens(
        self, auth: ConnectionAuth, current: ConnectionRecord | None
    ) -> dict[str, str | None]:
        tokens: dict[str, str | None] = {}
        for field in ("password", "passphrase"):
            value = getattr(auth, field)
            if value is KEEP:
                same_method = current is not None and current.auth == auth.method
                tokens[field] = getattr(current, field) if same_method else None
            elif isinstance(value, str):
                tokens[field] = await self.keys.encrypt_secret(value)
            else:
                tokens[field] = None
        return tokens

    def _read(self) -> ConnectionsFile:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ConnectionsFile()
        except OSError as e:
            raise ConnectionsCorruptedError(f"Cannot read {self.path}: {e}") from e
        try:
            content = ConnectionsFile.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError) as e:
            raise ConnectionsCorruptedError(f"{self.path} is damaged: {e}") from e
        if content.version != FILE_VERSION:
            raise ConnectionsCorruptedError(
                f"Unsupported connections file version {content.version}"
            )
        return content

    def _write(self, content: ConnectionsFile) -> None:
        write_private_file(self.path, content.model_dump_json(indent=2))

    @staticmethod
    def _find(content: ConnectionsFile, connection_id: str) -> ConnectionRecord:
        for record in content.connections:
            if record.id == connection_id:
                return record
        raise ConnectionNotFoundError(f"Connection '{connection_id}' not found")

    @staticmethod
    def _ensure_unique_name(
        content: ConnectionsFile, name: str, exclude_id: str | None = None
    ) -> None:
        for record in content.connections:
            if record.id != exclude_id and record.name.casefold() == name.casefold():
                raise ConnectionExistsError(f"Connection named '{name}' already exists")


_store: ConnectionStore | None = None


def get_connection_store() -> ConnectionStore:
    """Хранилище по пути из настроек (CONNECTIONS_PATH или папка данных пользователя)."""
    global _store
    path = settings.connections_path
    keys = get_keystore()
    if _store is None or _store.path != path or _store.keys is not keys:
        _store = ConnectionStore(path, keys)
    return _store


def check_auth(auth: ConnectionAuth, current: ConnectionRecord | None = None) -> None:
    """Поля способа входа согласованы с методом (KEEP: сохранённое у current)."""

    def given(field: str) -> bool:
        value = getattr(auth, field)
        if value is KEEP:
            same_method = current is not None and current.auth == auth.method
            return same_method and getattr(current, field) is not None
        return value is not None

    has_password, has_phrase = given("password"), given("passphrase")
    if auth.method == "key_id":
        if not auth.key_id:
            raise InvalidConnectionError("key_id is required for auth=key_id")
        if auth.key_path or has_password or has_phrase:
            raise InvalidConnectionError("auth=key_id takes only key_id")
    elif auth.method == "key_path":
        if not auth.key_path:
            raise InvalidConnectionError("key_path is required for auth=key_path")
        if auth.key_id or has_password:
            raise InvalidConnectionError("auth=key_path takes key_path and optional passphrase")
    else:
        if auth.key_id or auth.key_path or has_phrase:
            raise InvalidConnectionError("auth=password takes only an optional password")


def _now() -> datetime:
    return datetime.now(UTC)
