from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from app.plugins.connections.store import AuthMethod, ConnectionRecord

CONNECTION_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
AUTH_FIELDS = frozenset({"auth", "key_id", "key_path", "password", "passphrase"})
PLAIN_FIELDS = frozenset({"name", "host", "port", "username", "group"})


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError("must not be empty")
    if any(ord(ch) < 32 for ch in value):
        raise ValueError("must not contain control characters")
    return value


def _key_path(value: str | None) -> str | None:
    if value is None:
        return None
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        raise ValueError("must be an absolute path to the private key")
    if not path.is_file():
        raise ValueError(f"private key file not found: {path}")
    return str(path)


class _ConnectionFields(BaseModel):
    group: str | None = Field(
        default=None, max_length=100, description="Папка в списке подключений, например Work"
    )
    key_id: str | None = Field(
        default=None, min_length=1, max_length=64, description="ID ключа из /api/keys"
    )
    key_path: str | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
        description="Путь к приватному ключу на этой машине (можно с ~)",
    )
    password: SecretStr | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
        description="Пароль. Не передан: спрашивать при каждом подключении",
    )
    passphrase: SecretStr | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
        description="Фраза ключа из key_path, если он зашифрован",
    )

    _group = field_validator("group")(_clean_text)
    _key_path = field_validator("key_path")(_key_path)


class ConnectionCreateRequest(_ConnectionFields):
    name: str = Field(..., min_length=1, max_length=100, description="Уникальное имя")
    host: str = Field(..., min_length=1, max_length=255, description="IP или домен сервера")
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(..., min_length=1, max_length=64)
    auth: AuthMethod = Field(
        ...,
        description=(
            "key_id: ключ из хранилища; key_path: файл ключа (+ passphrase); "
            "password: пароль (сохранённый или при подключении)"
        ),
    )

    _text = field_validator("name", "host", "username")(_clean_text)


class ConnectionUpdateRequest(_ConnectionFields):
    """Только переданные поля. Способ входа меняется целиком через auth; без auth можно
    сменить или забыть (null) сохранённый пароль или фразу текущего способа."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, min_length=1, max_length=64)
    auth: AuthMethod | None = None

    _text = field_validator("name", "host", "username")(_clean_text)

    @model_validator(mode="after")
    def _required_fields_are_not_null(self) -> ConnectionUpdateRequest:
        for field in ("name", "host", "port", "username", "auth"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class ConnectionInfo(BaseModel):
    """Подключение без секретов: пароль и фразу API не отдаёт никогда."""

    id: str
    name: str
    host: str
    port: int
    username: str
    group: str | None
    auth: AuthMethod
    key_id: str | None
    key_path: str | None
    has_password: bool = Field(..., description="Пароль сохранён")
    has_passphrase: bool = Field(..., description="Фраза ключа сохранена")
    created_at: datetime
    updated_at: datetime
    last_connected_at: datetime | None

    @classmethod
    def from_record(cls, record: ConnectionRecord) -> ConnectionInfo:
        return cls(
            id=record.id,
            name=record.name,
            host=record.host,
            port=record.port,
            username=record.username,
            group=record.group,
            auth=record.auth,
            key_id=record.key_id,
            key_path=record.key_path,
            has_password=record.password is not None,
            has_passphrase=record.passphrase is not None,
            created_at=record.created_at,
            updated_at=record.updated_at,
            last_connected_at=record.last_connected_at,
        )


class ConnectionConnectRequest(BaseModel):
    """Секреты, которые не сохранены (или чтобы подключиться с другими)."""

    password: SecretStr | None = Field(
        default=None, min_length=1, max_length=1024, description="Пароль для auth=password"
    )
    passphrase: SecretStr | None = Field(
        default=None, min_length=1, max_length=1024, description="Фраза ключа для auth=key_path"
    )
