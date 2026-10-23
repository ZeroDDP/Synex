from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class SSHConnectRequest(BaseModel):
    host: str = Field(..., min_length=1, max_length=255, description="IP или домен сервера")
    port: int = Field(default=22, ge=1, le=65535, description="SSH-порт")
    username: str = Field(..., min_length=1, max_length=64, description="Имя пользователя")
    key_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="ID ключа из хранилища /api/keys. Указывается вместо key_path",
    )
    key_path: str | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
        description=(
            "Путь к приватному ключу на этой машине (воркер запущен здесь же). "
            "Можно с ~, например ~/.ssh/id_ed25519. Указывается вместо key_id"
        ),
        examples=["C:/Users/user/.ssh/id_ed25519"],
    )

    @field_validator("host", "username")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("key_path")
    @classmethod
    def _resolve_key_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = Path(value.strip()).expanduser()
        if not path.is_absolute():
            raise ValueError("must be an absolute path to the private key")
        if not path.is_file():
            raise ValueError(f"private key file not found: {path}")
        return str(path)

    @model_validator(mode="after")
    def _one_key_source(self) -> SSHConnectRequest:
        if (self.key_id is None) == (self.key_path is None):
            raise ValueError("pass exactly one of key_id or key_path")
        return self


class SSHConnectResponse(BaseModel):
    status: Literal["connected"] = "connected"
    message: str = Field(..., description="Ответ воркера (stdout)")
    host: str
    port: int
    username: str
    key_id: str | None = None


class SSHExecRequest(BaseModel):
    command: str = Field(..., min_length=1, max_length=4096, description="Команда для выполнения")
    timeout: float | None = Field(
        default=None,
        gt=0,
        le=3600,
        description=(
            "Лимит выполнения команды, сек (по умолчанию SSH_EXEC_TIMEOUT). По истечении "
            "команда прерывается на сервере, ответ приходит с timed_out=true"
        ),
    )
    track_cwd: bool = Field(
        default=True,
        description=(
            "Обёртка команды: сохранённая cwd, точный exit_code и таймаут на сервере. "
            "Нужен POSIX shell (bash/zsh/sh). false: команда уходит воркеру как есть"
        ),
    )


class SSHExecResponse(BaseModel):
    stdout: str = Field(default="", description="Стандартный вывод команды")
    stderr: str = Field(default="", description="Стандартный вывод ошибок")
    exit_code: int = Field(..., description="Код возврата (0 = успех; 124/137 при таймауте)")
    cwd: str | None = Field(default=None, description="Текущая директория после команды")
    timed_out: bool = Field(default=False, description="Команду прервал таймаут на сервере")
    duration_ms: int = Field(..., description="Время выполнения, мс")


class SSHStatusResponse(BaseModel):
    connected: bool
    host: str | None = None
    port: int | None = None
    username: str | None = None
    key_id: str | None = None
    cwd: str | None = None
