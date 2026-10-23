from __future__ import annotations

from typing import Any

import structlog

from app.core.locks import LoopLock
from app.plugins.ssh.wrapper import new_marker

logger = structlog.get_logger()


class SSHSessionState:
    """Состояние SSH-сессии со стороны Python.

    Сама сессия живёт внутри воркера (одна на процесс). Здесь хранится то, что воркер
    не помнит: к какому хосту подключились, каким ключом и текущая директория (cwd).
    """

    def __init__(self) -> None:
        self.is_connected: bool = False
        self.current_host: str | None = None
        self.username: str | None = None
        self.port: int | None = None
        self.key_id: str | None = None
        self.cwd: str | None = None  # None = домашняя папка, путь ещё не известен
        self.marker: str = new_marker()
        # Одна операция с сессией за раз. Воркер всё равно выполняет запросы по очереди;
        # без этой блокировки таймаут второй команды тикал бы, пока она ждёт в очереди
        # воркера, а cwd двух параллельных команд перетирали бы друг друга.
        self.lock = LoopLock()

    def mark_connected(
        self,
        host: str,
        username: str | None = None,
        port: int | None = None,
        key_id: str | None = None,
    ) -> None:
        self.is_connected = True
        self.current_host = host
        self.username = username
        self.port = port
        self.key_id = key_id
        self.cwd = None
        self.marker = new_marker()
        logger.info("SSH session state marked as connected", host=host)

    def mark_disconnected(self) -> None:
        was_connected = self.is_connected
        self.is_connected = False
        self.current_host = None
        self.username = None
        self.port = None
        self.key_id = None
        self.cwd = None
        if was_connected:
            logger.info("SSH session state marked as disconnected")

    def snapshot(self) -> dict[str, Any]:
        return {
            "connected": self.is_connected,
            "host": self.current_host,
            "port": self.port,
            "username": self.username,
            "key_id": self.key_id,
            "cwd": self.cwd,
        }


# Воркер держит одну SSH-сессию на процесс, поэтому и состояние здесь одно.
ssh_state = SSHSessionState()
