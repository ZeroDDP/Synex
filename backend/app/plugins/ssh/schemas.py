from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from app.plugins.ssh.known_hosts import FINGERPRINT_PATTERN

SESSION_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"


def session_id_field() -> Any:
    return Field(
        default=None,
        pattern=SESSION_ID_PATTERN,
        description="ID сессии из ответа /api/ssh/connect. Без него берётся последняя открытая",
    )


class SSHConnectRequest(BaseModel):
    host: str = Field(..., min_length=1, max_length=255, description="IP или домен сервера")
    port: int = Field(default=22, ge=1, le=65535, description="SSH-порт")
    username: str = Field(..., min_length=1, max_length=64, description="Имя пользователя")
    key_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="ID ключа из хранилища /api/keys",
    )
    key_path: str | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
        description=(
            "Путь к приватному ключу на этой машине (воркер запущен здесь же). "
            "Можно с ~, например ~/.ssh/id_ed25519"
        ),
        examples=["C:/Users/user/.ssh/id_ed25519"],
    )
    passphrase: SecretStr | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
        description="Парольная фраза ключа из key_path, если он зашифрован",
    )
    password: SecretStr | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
        description="Пароль пользователя (вход по паролю вместо ключа)",
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
    def _one_auth_method(self) -> SSHConnectRequest:
        given = [v for v in (self.key_id, self.key_path, self.password) if v is not None]
        if len(given) != 1:
            raise ValueError("pass exactly one of key_id, key_path or password")
        if self.passphrase is not None and self.key_path is None:
            raise ValueError(
                "passphrase is only used with key_path "
                "(keys in the key storage are already stored without a passphrase)"
            )
        return self


class SSHConnectResponse(BaseModel):
    status: Literal["connected"] = "connected"
    session_id: str = Field(..., description="ID сессии для exec, disconnect и status")
    message: str = Field(..., description="Ответ воркера (stdout)")
    host: str
    port: int
    username: str
    auth: Literal["key_id", "key_path", "password"]
    key_id: str | None = None


AuthMethodName = Literal["key_id", "key_path", "password"]


class HostKeyInfo(BaseModel):
    fingerprint: str = Field(..., description="Отпечаток ключа сервера, SHA256:<base64>")
    key_type: str = Field(default="", description="Тип ключа, например ssh-ed25519")


class SSHSessionInfo(BaseModel):
    session_id: str
    host: str
    port: int
    username: str
    auth: AuthMethodName
    key_id: str | None = None
    connection_id: str | None = Field(
        default=None, description="Сохранённое подключение (/api/connections), если из него"
    )
    host_key: HostKeyInfo | None = Field(
        default=None,
        description="Ключ сервера, с которым воркер подключился (null: сборка воркера без TOFU)",
    )
    cwd: str | None = Field(default=None, description="Текущая директория (None = домашняя)")
    created_at: datetime
    last_activity_at: datetime
    busy: bool = Field(..., description="Сейчас выполняется команда")
    waiting: int = Field(..., description="Сколько команд ждут своей очереди")
    attached: int = Field(..., description="Сколько WebSocket-подключений смотрят сессию")
    last_seq: int = Field(..., description="Номер последней записи в буфере вывода")
    channel: Literal["dedicated", "shared"] = Field(
        ...,
        description=(
            "dedicated: своё подключение к воркеру, команды идут параллельно с другими "
            "сессиями; shared: общее подключение, команды всех сессий по очереди"
        ),
    )


class SSHCommandRequest(BaseModel):
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
    allow_interactive: bool = Field(
        default=False,
        description=(
            "Отправить, даже если программе нужен терминал (vim, top, tmux): без этого "
            "такая команда отклоняется с 422 ssh_interactive_command и подсказкой"
        ),
    )


class SSHExecRequest(SSHCommandRequest):
    session_id: str | None = session_id_field()


class SSHExecResponse(BaseModel):
    session_id: str
    seq: int = Field(..., description="Номер записи с этим результатом в буфере вывода")
    stdout: str = Field(default="", description="Стандартный вывод команды")
    stderr: str = Field(default="", description="Стандартный вывод ошибок")
    exit_code: int = Field(..., description="Код возврата (0 = успех; 124/137 при таймауте)")
    cwd: str | None = Field(default=None, description="Текущая директория после команды")
    timed_out: bool = Field(default=False, description="Команду прервал таймаут на сервере")
    interrupted: bool = Field(default=False, description="Команду прервали (Ctrl+C в UI)")
    duration_ms: int = Field(..., description="Время выполнения, мс")


class SSHInterruptResponse(BaseModel):
    session_id: str
    status: Literal["interrupted", "idle", "finished", "alive"] = Field(
        ...,
        description=(
            "interrupted: команда остановлена; idle: ничего не выполнялось; finished: "
            "закончилась сама раньше сигнала; alive: не остановилась даже от KILL"
        ),
    )
    signal: Literal["INT", "TERM", "KILL"] | None = Field(
        ..., description="Каким сигналом остановлена: INT (как Ctrl+C), потом TERM, KILL"
    )
    command: str | None = Field(..., description="Какую команду прерывали")


class SSHHistoryResponse(BaseModel):
    session_id: str
    server: str = Field(..., description="user@host:port: история общая для его сессий")
    commands: list[str] = Field(..., description="Старые первыми")


class SSHHistoryCleared(BaseModel):
    server: str
    removed: int


class SSHCompletionResponse(BaseModel):
    word: str = Field(..., description="Что дополняли (слово под курсором)")
    start: int = Field(..., description="Где слово начинается в строке: его заменяет вставка")
    kind: Literal["command", "path"]
    candidates: list[str] = Field(..., description="Варианты; у папок / в конце")
    insert: list[str] = Field(..., description="Те же варианты с экранированными пробелами")
    common: str = Field(..., description="Общее начало вариантов: можно вставить сразу")
    truncated: bool = Field(..., description="Вариантов больше 200")


class SSHDisconnectRequest(BaseModel):
    session_id: str | None = session_id_field()


class SSHDisconnectResponse(BaseModel):
    status: Literal["disconnected"] = "disconnected"
    session_id: str
    message: str = Field(..., description="Ответ воркера")


class SSHStatusResponse(BaseModel):
    connected: bool
    session_id: str | None = None
    host: str | None = None
    port: int | None = None
    username: str | None = None
    auth: Literal["key_id", "key_path", "password"] | None = None
    key_id: str | None = None
    cwd: str | None = None
    created_at: datetime | None = None
    sessions: int = Field(default=0, description="Сколько сессий открыто всего")


class SSHOutputEntry(BaseModel):
    """Запись буфера вывода. Поля, кроме seq/ts/kind, зависят от kind:
    opened {host, port, username}; command {command, cwd, source, request_id?};
    result {stdout, stderr, exit_code, cwd, timed_out, interrupted, duration_ms,
    request_id?, truncated?}; error {error, detail, request_id?}."""

    model_config = {"extra": "allow"}

    seq: int
    ts: int = Field(..., description="Unix, мс")
    kind: Literal["opened", "command", "result", "error"]


class SSHOutputResponse(BaseModel):
    session_id: str
    entries: list[SSHOutputEntry]
    gap: bool = Field(..., description="Часть записей после since уже вытеснена из буфера")
    first_seq: int = Field(..., description="Самая старая запись в буфере")
    last_seq: int = Field(..., description="Последняя запись в буфере")


# ------------------------------------------------------------ known hosts (TOFU)


class KnownHostInfo(BaseModel):
    host: str
    port: int
    fingerprint: str = Field(..., description="Подтверждённый отпечаток, SHA256:<base64>")
    key_type: str | None = None
    source: Literal["user", "openssh"] = Field(
        default="user",
        description=(
            "Кто подтвердил ключ: user — пользователь в Synex; openssh — ключ был записан в "
            "known_hosts OpenSSH на этом компьютере, и backend взял его оттуда"
        ),
    )
    added_at: datetime
    updated_at: datetime


class KnownHostRequest(BaseModel):
    host: str = Field(..., min_length=1, max_length=255, description="IP или домен сервера")
    port: int = Field(default=22, ge=1, le=65535)
    fingerprint: str = Field(
        ...,
        pattern=FINGERPRINT_PATTERN,
        description="Отпечаток, который подтвердил пользователь (из host_key в ответе 409)",
    )
    key_type: str | None = Field(default=None, max_length=64, description="Тип ключа из 409")
    replace: bool = Field(
        default=False,
        description=(
            "Заменить другой сохранённый отпечаток. Только по явному действию пользователя: "
            "ключ сервера сменился (переустановка) и пользователь это подтвердил"
        ),
    )

    @field_validator("host")
    @classmethod
    def _strip_host(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class KnownHostChange(BaseModel):
    changed: bool = Field(..., description="Запись появилась или изменилась")
    known_host: KnownHostInfo
