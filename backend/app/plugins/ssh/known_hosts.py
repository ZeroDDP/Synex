"""Доверенные ключи SSH-серверов (схема TOFU): known_hosts.json в папке данных.

Воркер (сборки с 2026-11-14) подключается к серверу, только если ему передали
отпечаток ключа сервера, и этот отпечаток совпал:

  * отпечатка нет: ответ host_key_unknown с отпечатком и типом ключа сервера. Backend
    отвечает 409 ssh_host_key_unknown; UI показывает отпечаток пользователю и после
    подтверждения сохраняет его (POST /api/ssh/known-hosts), затем подключается снова;
  * отпечаток не совпал: host_key_mismatch. Backend отвечает 409 ssh_host_key_mismatch
    и сам не повторяет подключение. Сохранённый отпечаток меняется только явным
    действием пользователя (POST /api/ssh/known-hosts с replace=true).

Ключ, которому уже доверяет OpenSSH на этом компьютере (он записан в ~/.ssh/known_hosts),
подтверждать ещё раз не нужно: backend находит его там и сохраняет сам, с source
"openssh" (openssh_hosts.py).

Формат файла:

    {
      "version": 1,
      "hosts": [
        {"host", "port", "fingerprint": "SHA256:...", "key_type": "ssh-ed25519",
         "source": "user" | "openssh", "added_at", "updated_at"}
      ]
    }

Отпечатки не секрет, но файл пишется так же, как остальные данные пользователя:
атомарно и с правами только для владельца (app/core/files.py).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import structlog
from pydantic import BaseModel, Field, ValidationError

from app.core.config import settings
from app.core.errors import AppError
from app.core.files import write_private_file
from app.core.locks import LoopLock

logger = structlog.get_logger()

FILE_VERSION = 1
# Формат OpenSSH: SHA256 и base64 без "=" в конце (43 символа для SHA-256)
FINGERPRINT_PATTERN = r"^SHA256:[A-Za-z0-9+/]{43}$"
_FINGERPRINT = re.compile(FINGERPRINT_PATTERN)

# Кто подтвердил ключ: пользователь в Synex или запись в known_hosts OpenSSH
Source = Literal["user", "openssh"]


class KnownHostsCorruptedError(AppError):
    code = "known_hosts_corrupted"
    status_code = 500


class KnownHostNotFoundError(AppError):
    code = "ssh_known_host_not_found"
    status_code = 404


class KnownHostConflictError(AppError):
    """Для сервера уже сохранён другой отпечаток, а replace не передан."""

    code = "ssh_known_host_conflict"
    status_code = 409


class KnownHost(BaseModel):
    host: str
    port: int = 22
    fingerprint: str
    key_type: str | None = None
    source: Source = "user"  # в файлах до появления поля все записи от пользователя
    added_at: datetime
    updated_at: datetime


class KnownHostsFile(BaseModel):
    version: int = FILE_VERSION
    hosts: list[KnownHost] = Field(default_factory=list)


def normalize_host(host: str) -> str:
    """Один сервер под одним именем: без пробелов и регистра, IPv6 без скобок."""
    host = host.strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host


def server_name(host: str, port: int) -> str:
    """host:port для сообщений; IPv6 в скобках."""
    host = normalize_host(host)
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def is_fingerprint(value: str) -> bool:
    return bool(_FINGERPRINT.match(value))


class KnownHostsStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = LoopLock()

    async def list_hosts(self) -> list[KnownHost]:
        async with self._lock:
            return list(self._read().hosts)

    async def get(self, host: str, port: int) -> KnownHost | None:
        async with self._lock:
            return self._find(self._read(), host, port)

    async def fingerprint(self, host: str, port: int) -> str | None:
        known = await self.get(host, port)
        return known.fingerprint if known is not None else None

    async def trust(
        self,
        host: str,
        port: int,
        fingerprint: str,
        key_type: str | None = None,
        *,
        replace: bool = False,
        source: Source = "user",
    ) -> tuple[KnownHost, bool]:
        """Сохранить отпечаток, который подтвердил пользователь (source "openssh": ключ
        найден в known_hosts OpenSSH). Возвращает (запись, изменилось ли что-то). Другой
        отпечаток уже сохранён и replace=False: KnownHostConflictError."""
        if not is_fingerprint(fingerprint):
            raise ValueError(f"not an OpenSSH SHA256 fingerprint: {fingerprint!r}")
        async with self._lock:
            content = self._read()
            current = self._find(content, host, port)
            now = _now()
            if current is not None:
                if current.fingerprint == fingerprint:
                    # Тот же ключ: уточняется тип, а запись из OpenSSH, которую теперь
                    # подтвердил сам пользователь, становится его записью
                    new_type = bool(key_type) and current.key_type != key_type
                    confirmed = source == "user" and current.source != "user"
                    if not new_type and not confirmed:
                        return current, False
                    if new_type:
                        current.key_type = key_type
                    if confirmed:
                        current.source = "user"
                    current.updated_at = now
                    self._write(content)
                    return current, True
                if not replace:
                    error = KnownHostConflictError(
                        f"Server {server_name(host, port)} is already trusted with another "
                        f"key ({current.fingerprint}). To trust {fingerprint} instead, "
                        "repeat with replace=true"
                    )
                    error.extra = {
                        "host_key": {
                            "host": current.host,
                            "port": current.port,
                            "fingerprint": fingerprint,
                            "expected_fingerprint": current.fingerprint,
                        }
                    }
                    raise error
                previous = current.fingerprint
                current.fingerprint = fingerprint
                current.key_type = key_type
                current.source = source
                current.updated_at = now
                self._write(content)
                logger.warning(
                    "SSH host key replaced by the user",
                    server=server_name(host, port),
                    previous=previous,
                    fingerprint=fingerprint,
                )
                return current, True
            record = KnownHost(
                host=normalize_host(host),
                port=port,
                fingerprint=fingerprint,
                key_type=key_type,
                source=source,
                added_at=now,
                updated_at=now,
            )
            content.hosts.append(record)
            self._write(content)
        logger.info(
            "SSH host key trusted",
            server=server_name(host, port),
            fingerprint=fingerprint,
            source=source,
        )
        return record, True

    async def remove(self, host: str, port: int) -> KnownHost:
        async with self._lock:
            content = self._read()
            current = self._find(content, host, port)
            if current is None:
                raise KnownHostNotFoundError(f"No trusted key for {server_name(host, port)}")
            content.hosts.remove(current)
            self._write(content)
        logger.info("SSH host key forgotten", server=server_name(host, port))
        return current

    # ------------------------------------------------------------- internals

    def _read(self) -> KnownHostsFile:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return KnownHostsFile()
        except OSError as e:
            raise KnownHostsCorruptedError(f"Cannot read {self.path}: {e}") from e
        try:
            content = KnownHostsFile.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError) as e:
            raise KnownHostsCorruptedError(f"{self.path} is damaged: {e}") from e
        if content.version != FILE_VERSION:
            raise KnownHostsCorruptedError(
                f"Unsupported known hosts file version {content.version}"
            )
        return content

    def _write(self, content: KnownHostsFile) -> None:
        write_private_file(self.path, content.model_dump_json(indent=2))

    @staticmethod
    def _find(content: KnownHostsFile, host: str, port: int) -> KnownHost | None:
        host = normalize_host(host)
        for record in content.hosts:
            if record.host == host and record.port == port:
                return record
        return None


def _now() -> datetime:
    return datetime.now(UTC)


_store: KnownHostsStore | None = None


def get_known_hosts() -> KnownHostsStore:
    """Хранилище по пути из настроек (KNOWN_HOSTS_PATH или папка данных пользователя)."""
    global _store
    path = settings.known_hosts_path
    if _store is None or _store.path != path:
        _store = KnownHostsStore(path)
    return _store
