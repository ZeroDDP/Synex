from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from app.keystore import KeyRecord

NAME_FIELD = Field(..., min_length=1, max_length=64, description="Уникальное имя ключа")
COMMENT_FIELD = Field(default="", max_length=256, description="Комментарий в публичном ключе")


def _clean_name(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("must not be empty")
    if any(ord(ch) < 32 for ch in value):
        raise ValueError("must not contain control characters")
    return value


def _clean_comment(value: str) -> str:
    value = value.strip()
    if any(ord(ch) < 32 for ch in value):
        raise ValueError("must not contain control characters")
    return value


class KeyInfo(BaseModel):
    """Ключ без секретной части: приватный ключ API не отдаёт никогда."""

    id: str
    name: str
    type: str = Field(..., description='Тип ключа, например "ssh-ed25519"')
    bits: int
    fingerprint: str = Field(..., description="SHA256-отпечаток, как в `ssh-keygen -lf`")
    public_key: str = Field(..., description="Строка для ~/.ssh/authorized_keys на сервере")
    comment: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: KeyRecord) -> KeyInfo:
        return cls(
            id=record.id,
            name=record.name,
            type=record.key_type,
            bits=record.bits,
            fingerprint=record.fingerprint,
            public_key=record.public_key_line,
            comment=record.comment,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class KeyImportRequest(BaseModel):
    name: str = NAME_FIELD
    private_key: SecretStr | None = Field(
        default=None,
        description="Содержимое приватного ключа (OpenSSH или PEM). Указывается вместо пути",
    )
    private_key_path: str | None = Field(
        default=None,
        max_length=1024,
        description="Путь к файлу ключа на этой машине (можно с ~). Указывается вместо содержимого",
    )
    passphrase: SecretStr | None = Field(
        default=None,
        description=(
            "Парольная фраза ключа, если есть. Не сохраняется: ключ хранится "
            "расшифрованным под мастер-ключом хранилища"
        ),
    )
    comment: str = COMMENT_FIELD

    _name = field_validator("name")(_clean_name)
    _comment = field_validator("comment")(_clean_comment)

    @field_validator("private_key_path")
    @classmethod
    def _resolve_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = Path(value.strip()).expanduser()
        if not path.is_absolute():
            raise ValueError("must be an absolute path")
        if not path.is_file():
            raise ValueError(f"file not found: {path}")
        return str(path)

    @model_validator(mode="after")
    def _one_source(self) -> KeyImportRequest:
        if (self.private_key is None) == (self.private_key_path is None):
            raise ValueError("pass exactly one of private_key or private_key_path")
        return self


class KeyGenerateRequest(BaseModel):
    name: str = NAME_FIELD
    type: Literal["ed25519", "rsa"] = Field(default="ed25519", description="Тип нового ключа")
    bits: int | None = Field(
        default=None, ge=2048, le=8192, description="Только для RSA, по умолчанию 4096"
    )
    comment: str = COMMENT_FIELD

    _name = field_validator("name")(_clean_name)
    _comment = field_validator("comment")(_clean_comment)

    @model_validator(mode="after")
    def _bits_only_for_rsa(self) -> KeyGenerateRequest:
        if self.type != "rsa" and self.bits is not None:
            raise ValueError("bits can be set only for RSA keys")
        return self


class KeyUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    comment: str | None = Field(default=None, max_length=256)

    @field_validator("name")
    @classmethod
    def _name_clean(cls, value: str | None) -> str | None:
        return None if value is None else _clean_name(value)

    @field_validator("comment")
    @classmethod
    def _comment_clean(cls, value: str | None) -> str | None:
        return None if value is None else _clean_comment(value)

    @model_validator(mode="after")
    def _not_empty(self) -> KeyUpdateRequest:
        if self.name is None and self.comment is None:
            raise ValueError("nothing to update: pass name and/or comment")
        return self
