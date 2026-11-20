"""Логи сервера: в какой SSH-сессии выполнять команды и как понимать их ответ.

Сервер задаётся так же, как в метриках и алертах: "host:port" (ServerMetrics.server). UI
выбирает сервер на Dashboard и не думает о сессиях: backend берёт открытую сессию к этому
серверу, свободную от команды пользователя, если такая есть.

Разовые запросы (REST) идут служебными командами сессии (run_service): в терминал не
попадают и ждут команду пользователя не дольше SSH_SERVICE_WAIT. Живой лог (WebSocket)
открывает свою служебную сессию (LOGS_COMPANION): опрос раз в LOGS_INTERVAL не стоит в
очереди за долгой командой в терминале. Если воркер не даёт дополнительное подключение,
живой лог работает в основной сессии.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.core.config import settings
from app.core.errors import AppError
from app.plugins.logs import files as logfiles
from app.plugins.logs import journal
from app.plugins.logs.errors import (
    LogFileDeniedError,
    LogFileIsDirectoryError,
    LogFileNotFoundError,
    LogsBadFilterError,
    LogsCommandError,
    LogsJournalUnavailableError,
    LogsServerNotConnectedError,
    LogsTimeoutError,
)
from app.plugins.ssh.errors import SSHNotConnectedError, SSHSessionNotFoundError
from app.plugins.ssh.manager import ServiceResult, SSHSessionManager
from app.plugins.ssh.session import CompanionSession, SSHSession

logger = structlog.get_logger()

# journalctl не принял фильтр (тексты systemd 219 ... 256)
_BAD_FILTER = (
    "failed to parse timestamp",
    "unknown log level",
    "bad pattern",
    "failed to seek to cursor",
    "failed to add match",
    "failed to parse",
)

# Сервер не умеет --output-fields или -g: запоминаем, чтобы не пробовать каждый раз
_no_output_fields: set[str] = set()
_no_grep: set[str] = set()


def server_key(host: str, port: int) -> str:
    """Как ServerMetrics.server в метриках (app/plugins/monitor/aggregator.py)."""
    return f"{host.lower()}:{port}"


def resolve_session(
    manager: SSHSessionManager, server: str, session_id: str | None = None
) -> SSHSession:
    """Сессия к серверу server. session_id: именно эта (должна идти к этому серверу)."""
    wanted = server.lower()
    sessions = [
        s for s in manager.sessions() if not s.closed and server_key(s.host, s.port) == wanted
    ]
    if session_id is not None:
        for session in sessions:
            if session.id == session_id:
                return session
        raise LogsServerNotConnectedError(
            f"SSH session '{session_id}' is not open or does not go to {server}"
        )
    if not sessions:
        raise LogsServerNotConnectedError(
            f"No open SSH session to {server}: connect to the server first"
        )
    idle = [s for s in sessions if not s.channel.lock.locked()]
    return (idle or sessions)[0]


class LogRunner:
    """Команды логов одного сервера.

    companion=True (живой лог): первая команда открывает служебную сессию; не вышло, или
    она потерялась — команды идут в основную. Сессию, выбранную без session_id, при
    закрытии заменяет другая сессия к тому же серверу."""

    def __init__(
        self,
        manager: SSHSessionManager,
        server: str,
        session_id: str | None = None,
        *,
        companion: bool = False,
        wait: float | None = None,
    ) -> None:
        self.manager = manager
        self.server = server
        self.pinned = session_id
        self.wait = wait
        self.use_companion = companion and settings.LOGS_COMPANION
        self.session = resolve_session(manager, server, session_id)
        self._companion: CompanionSession | None = None
        self._companion_tried = False

    @property
    def key(self) -> str:
        return self.server.lower()

    @property
    def session_id(self) -> str:
        return self.session.id

    @property
    def companion(self) -> bool:
        return self._companion is not None and not self._companion.closed

    def _current(self) -> SSHSession:
        if self.session.closed:
            if self._companion is not None:
                self._companion = None
                self._companion_tried = False
            # Закрытую сессию заменяет другая к тому же серверу (если её не выбрали явно)
            self.session = resolve_session(self.manager, self.server, self.pinned)
        return self.session

    async def prepare(self) -> None:
        """Открыть служебную сессию (живой лог). Не вышло: команды пойдут в основную."""
        if not self.use_companion or self._companion_tried:
            return
        self._companion_tried = True
        session = self._current()
        try:
            self._companion = await self.manager.open_companion(session.id, "logs")
        except Exception as e:  # служебная не открылась: работаем в основной
            logger.info("Logs work in the main SSH session", reason=repr(e))

    async def run(self, command: str, *, timeout: float | None = None) -> ServiceResult:
        session = self._current()
        limit = timeout or settings.LOGS_TIMEOUT
        await self.prepare()
        if self._companion is not None and not self._companion.closed:
            try:
                return await self.manager.companion_run_service(
                    self._companion, command, timeout=limit
                )
            except SSHNotConnectedError:
                if session.closed:
                    raise
                self._companion = None  # потерялась сама служебная: дальше в основной
        return await self.manager.run_service(
            session.id, command, timeout=limit, wait=self.wait, name="logs"
        )

    async def close(self) -> None:
        companion, self._companion = self._companion, None
        if companion is not None:
            await self.manager.close_companion(companion)


GONE = (SSHSessionNotFoundError, SSHNotConnectedError, LogsServerNotConnectedError)


# ------------------------------------------------------------------ journal


def classify_journal(result: ServiceResult) -> AppError | None:
    if result.timed_out:
        return LogsTimeoutError(
            f"journalctl did not finish within {result.duration_ms} ms: narrow the search "
            "(time, unit, level) or raise LOGS_TIMEOUT"
        )
    if result.exit_code == 0:
        return None
    if result.exit_code == 1 and not result.stderr.strip() and not result.stdout.strip():
        return None  # с -g journalctl отвечает 1, если ничего не нашёл
    message = (
        result.stderr.strip()
        or result.stdout.strip()
        or f"journalctl exited with {result.exit_code}"
    )
    lower = message.lower()
    if result.exit_code == 127 or "journalctl: not found" in lower or "command not found" in lower:
        return LogsJournalUnavailableError(
            f"journalctl is not available on the server (not systemd?): {message}"
        )
    if any(text in lower for text in _BAD_FILTER):
        return LogsBadFilterError(message)
    return LogsCommandError(message)


async def _journal(
    runner: LogRunner, build: Any, query: journal.JournalQuery
) -> tuple[ServiceResult, bool]:
    """Выполнить команду journalctl; на старом journalctl повторить без --output-fields
    или без -g. Возвращает (результат, был ли поиск на сервере)."""
    output_fields = runner.key not in _no_output_fields
    grep = runner.key not in _no_grep
    while True:
        result = await runner.run(build(output_fields=output_fields, grep=grep))
        if result.exit_code != 0 and not result.timed_out:
            if output_fields and journal.lacks_output_fields(result.stderr):
                _no_output_fields.add(runner.key)
                output_fields = False
                continue
            if grep and query.grep and journal.lacks_grep(result.stderr):
                _no_grep.add(runner.key)
                grep = False
                continue
        break
    error = classify_journal(result)
    if error is not None:
        raise error
    return result, grep


async def journal_page(
    runner: LogRunner, query: journal.JournalQuery, lines: int, before: str | None = None
) -> list[journal.JournalEntry]:
    """Последние `lines` записей (или старше курсора before), старые первыми."""

    def build(*, output_fields: bool, grep: bool) -> str:
        # Без поиска на сервере (старый journalctl) берём больше записей и ищем сами
        count = lines if grep or not query.grep else min(lines * 20, settings.LOGS_MAX_LINES * 4)
        return journal.page_command(query, count, before, output_fields=output_fields, grep=grep)

    result, searched = await _journal(runner, build, query)
    entries = journal.parse_entries(result.stdout, newest_first=True)
    if query.grep and not searched:
        entries = [e for e in entries if journal.matches(e, query.grep)][-lines:]
    return entries


async def journal_follow(
    runner: LogRunner, query: journal.JournalQuery, after: str, limit: int
) -> list[journal.JournalEntry]:
    """Записи после курсора after, старые первыми (не больше limit)."""

    def build(*, output_fields: bool, grep: bool) -> str:
        return journal.follow_command(query, after, limit, output_fields=output_fields, grep=grep)

    result, searched = await _journal(runner, build, query)
    entries = journal.parse_entries(result.stdout)
    if query.grep and not searched:
        entries = [e for e in entries if journal.matches(e, query.grep)]
    return entries[:limit]


async def journal_tail_cursor(runner: LogRunner) -> str | None:
    result = await runner.run(journal.tail_cursor_command())
    error = classify_journal(result)
    if error is not None:
        raise error
    return journal.parse_cursor(result.stdout)


async def journal_units(runner: LogRunner) -> list[str]:
    result = await runner.run(journal.units_command())
    error = classify_journal(result)
    if error is not None:
        raise error
    return journal.parse_units(result.stdout)


async def journal_probe(runner: LogRunner) -> journal.JournalProbe:
    result = await runner.run(journal.probe_command())
    if result.timed_out:
        raise LogsTimeoutError(f"journalctl did not answer within {result.duration_ms} ms")
    return journal.parse_probe(result.stdout, result.stderr, result.exit_code)


# -------------------------------------------------------------------- files


def classify_file_status(status: str, path: str) -> AppError | None:
    """Ошибка по заголовку ответа скрипта (files.py)."""
    if status == "missing":
        return LogFileNotFoundError(f"{path} does not exist on the server")
    if status == "denied":
        return LogFileDeniedError(
            f"The SSH user cannot read {path}: system logs usually need the adm group "
            "(sudo usermod -aG adm <user>, then reconnect)"
        )
    if status == "directory":
        return LogFileIsDirectoryError(f"{path} is a folder: pick a file in it")
    return None


def classify_file(result: ServiceResult, status: str, path: str) -> AppError | None:
    if result.timed_out:
        return LogsTimeoutError(
            f"Reading {path} did not finish within {result.duration_ms} ms: raise LOGS_TIMEOUT"
        )
    if status == "ok":
        return None
    if not result.stdout.startswith(logfiles.HEADER):
        # Скрипт не дошёл до заголовка: ошибка shell или программы
        message = result.stderr.strip() or f"the command exited with {result.exit_code}"
        return LogsCommandError(f"Cannot read {path}: {message}")
    return classify_file_status(status, path)


async def list_files(runner: LogRunner, directory: str) -> list[logfiles.LogFile]:
    result = await runner.run(logfiles.list_command(directory))
    status, items = logfiles.parse_list(result.stdout)
    error = classify_file(result, status, directory)
    if error is not None:
        raise error
    return items


async def read_tail(
    runner: LogRunner, path: str, lines: int, grep: str | None = None
) -> logfiles.Chunk:
    """Последние `lines` строк файла (с текстом grep)."""
    if grep:
        result = await runner.run(logfiles.grep_command(path, grep, lines))
        chunk = logfiles.parse_grep(result.stdout)
    else:
        window = settings.LOGS_CHUNK_BYTES if lines else 0
        result = await runner.run(logfiles.tail_command(path, window))
        chunk = logfiles.parse_chunk(result.stdout, hold_partial=False)
    error = classify_file(result, chunk.status, path)
    if error is not None:
        raise error
    chunk.lines = chunk.lines[-lines:] if lines else []
    return chunk


async def read_follow(
    runner: LogRunner, path: str, offset: int, inode: str | None
) -> logfiles.Chunk:
    """Новые строки файла после offset. Файла нет или не прочитать: chunk.status, без
    исключения (при ротации файл ненадолго пропадает, решает вызывающий)."""
    chunk_bytes = settings.LOGS_CHUNK_BYTES
    result = await runner.run(logfiles.follow_command(path, offset, inode, chunk_bytes))
    chunk = logfiles.parse_chunk(result.stdout, hold_partial=True, limit=chunk_bytes)
    if result.timed_out or not result.stdout.startswith(logfiles.HEADER):
        error = classify_file(result, chunk.status, path)
        if error is not None:
            raise error
    return chunk
