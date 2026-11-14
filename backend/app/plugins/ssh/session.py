"""SSH-сессия со стороны Python.

Сама сессия живёт внутри воркера, её ключ там это id запросов (см. app/worker/client.py).
Здесь хранится то, что воркер не помнит: куда подключились и каким способом, текущая
директория (cwd), буфер вывода и кто сейчас смотрит сессию (WebSocket-подключения).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import structlog

from app.core.locks import LoopLock
from app.plugins.ssh.buffer import EntryKind, OutputBuffer, OutputEntry, Replay
from app.plugins.ssh.wrapper import new_marker
from app.worker import WorkerClient

logger = structlog.get_logger()

AuthMethod = Literal["key_id", "key_path", "password"]


def new_session_id() -> str:
    return secrets.token_hex(8)


def _now() -> datetime:
    return datetime.now(UTC)


class Subscriber(Protocol):
    """Тот, кто смотрит сессию: WebSocket-подключение UI. Методы не должны блокировать."""

    def deliver(self, entry: OutputEntry) -> None: ...

    def session_closed(self, reason: str) -> None: ...


@dataclass(eq=False)
class WorkerChannel:
    """IPC-подключение к воркеру, через которое работает сессия.

    Воркер выполняет запросы подключения по очереди, поэтому запросы идут по одному
    (lock): иначе таймаут команды тикал бы, пока она стоит в очереди воркера.
    dedicated: подключение принадлежит одной сессии и закрывается вместе с ней;
    иначе это общее основное подключение backend (запасной вариант).
    """

    client: WorkerClient
    dedicated: bool
    lock: LoopLock = field(default_factory=LoopLock)


@dataclass(eq=False)
class CompanionSession:
    """Служебная SSH-сессия к тому же серверу, что и сессия parent_id (например, для
    передачи файла): своё IPC-подключение, поэтому основная сессия во время долгой
    операции свободна. Не видна в списке сессий и закрывается вместе с основной."""

    id: str
    parent_id: str
    purpose: str
    channel: WorkerChannel
    closed: bool = False


@dataclass(eq=False)
class SSHSession:
    id: str
    host: str
    port: int
    username: str
    auth: AuthMethod
    channel: WorkerChannel
    buffer: OutputBuffer
    key_id: str | None = None
    connection_id: str | None = None  # сохранённое подключение, из которого открыта
    # Ключ сервера, с которым воркер подключился: {"fingerprint", "key_type"}.
    # None у сборок воркера без проверки ключа (до 2026-11-14).
    host_key: dict[str, str] | None = None
    cwd: str | None = None  # None = домашняя папка, путь ещё не известен
    marker: str = field(default_factory=new_marker)
    created_at: datetime = field(default_factory=_now)
    last_activity_at: datetime = field(default_factory=_now)
    running: str | None = None  # команда, которая сейчас выполняется
    waiting: int = 0  # команды в очереди за ней
    closed: bool = False
    close_reason: str | None = None
    subscribers: set[Subscriber] = field(default_factory=set)
    # Как подключались (ConnectParams и KeyStore): нужны, чтобы открыть к тому же
    # серверу служебную сессию (передача файлов). Только в памяти, наружу не отдаются.
    connect_params: Any = field(default=None, repr=False)
    keys: Any = field(default=None, repr=False)

    def info(self) -> dict[str, Any]:
        return {
            "session_id": self.id,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "auth": self.auth,
            "key_id": self.key_id,
            "connection_id": self.connection_id,
            "host_key": self.host_key,
            "cwd": self.cwd,
            "created_at": self.created_at,
            "last_activity_at": self.last_activity_at,
            "busy": self.running is not None,
            "waiting": self.waiting,
            "attached": len(self.subscribers),
            "last_seq": self.buffer.last_seq,
            "channel": "dedicated" if self.channel.dedicated else "shared",
        }

    def record(self, kind: EntryKind, data: dict[str, Any]) -> OutputEntry:
        """Записать в буфер и сразу отдать всем, кто смотрит сессию."""
        self.last_activity_at = _now()
        entry = self.buffer.append(kind, data)
        for subscriber in list(self.subscribers):
            try:
                subscriber.deliver(entry)
            except Exception as e:
                logger.warning("SSH subscriber failed", session_id=self.id, error=repr(e))
        return entry

    def attach(self, subscriber: Subscriber, since: int = 0) -> Replay:
        """Подписать на новые записи и вернуть пропущенные после since.

        Без await внутри: между снимком буфера и подпиской не может появиться запись,
        которая потеряется или придёт дважды.
        """
        replay = self.buffer.since(since)
        self.subscribers.add(subscriber)
        return replay

    def detach(self, subscriber: Subscriber) -> None:
        self.subscribers.discard(subscriber)

    def mark_closed(self, reason: str) -> None:
        if self.closed:
            return
        self.closed = True
        self.close_reason = reason
        subscribers, self.subscribers = list(self.subscribers), set()
        for subscriber in subscribers:
            try:
                subscriber.session_closed(reason)
            except Exception as e:
                logger.warning("SSH subscriber failed", session_id=self.id, error=repr(e))
