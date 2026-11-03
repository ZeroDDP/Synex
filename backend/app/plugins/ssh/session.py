"""SSH-сессии со стороны Python.

Сама сессия живёт внутри воркера, её ключ там это id запросов (см. app/worker/client.py).
Здесь хранится то, что воркер не помнит: куда подключились, каким способом и текущая
директория (cwd), которую обёртка подставляет в каждую команду.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import structlog

from app.plugins.ssh.errors import SSHNotConnectedError, SSHSessionNotFoundError
from app.plugins.ssh.wrapper import new_marker

logger = structlog.get_logger()

AuthMethod = Literal["key_id", "key_path", "password"]


def new_session_id() -> str:
    return secrets.token_hex(8)


@dataclass
class SSHSession:
    id: str
    host: str
    port: int
    username: str
    auth: AuthMethod
    key_id: str | None = None
    cwd: str | None = None  # None = домашняя папка, путь ещё не известен
    marker: str = field(default_factory=new_marker)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self.id,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "auth": self.auth,
            "key_id": self.key_id,
            "cwd": self.cwd,
            "created_at": self.created_at,
        }


class SSHSessionRegistry:
    """Открытые сессии. «Текущая» = последняя открытая: её используют запросы без
    session_id, так старый API с одной сессией работает как раньше."""

    def __init__(self) -> None:
        self._sessions: dict[str, SSHSession] = {}

    def add(self, session: SSHSession) -> None:
        self._sessions[session.id] = session
        logger.info(
            "SSH session opened",
            session_id=session.id,
            host=session.host,
            username=session.username,
            auth=session.auth,
        )

    def get(self, session_id: str) -> SSHSession:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise SSHSessionNotFoundError(f"SSH session '{session_id}' not found") from None

    def resolve(self, session_id: str | None) -> SSHSession:
        """Сессия по id или текущая, если id не передан."""
        if session_id is not None:
            return self.get(session_id)
        current = self.current
        if current is None:
            raise SSHNotConnectedError("SSH session is not open. Call /api/ssh/connect first.")
        return current

    @property
    def current(self) -> SSHSession | None:
        return next(reversed(self._sessions.values()), None)

    def contains(self, session_id: str) -> bool:
        return session_id in self._sessions

    def remove(self, session_id: str, *, reason: str) -> SSHSession | None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            logger.info("SSH session closed", session_id=session_id, reason=reason)
        return session

    def clear(self, *, reason: str) -> list[SSHSession]:
        sessions, self._sessions = list(self._sessions.values()), {}
        if sessions:
            logger.info("SSH sessions closed", count=len(sessions), reason=reason)
        return sessions

    def __iter__(self) -> Iterator[SSHSession]:
        return iter(list(self._sessions.values()))

    def __len__(self) -> int:
        return len(self._sessions)


ssh_sessions = SSHSessionRegistry()
