"""/api/logs/servers/{server}/...: логи сервера, выбранного на Dashboard.

{server} — "host:port", как ServerMetrics.server в /ws (в URL: encodeURIComponent).
?session_id= выбирает конкретную сессию к серверу (по умолчанию свободная из открытых).
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, WebSocket

from app.core.config import settings
from app.core.errors import AppError
from app.plugins.logs import journal, service
from app.plugins.logs.errors import LogsBadFilterError
from app.plugins.logs.schemas import (
    CURSOR_PATTERN,
    GREP_MAX,
    PATH_PATTERN,
    SERVER_PATTERN,
    TIME_PATTERN,
    UNIT_PATTERN,
    FileTail,
    JournalEntryInfo,
    JournalInfo,
    JournalPage,
    JournalUnits,
    Level,
    LogFileInfo,
    LogFiles,
    LogSources,
    Severity,
    priority_number,
)
from app.plugins.logs.stream import FileSource, JournalSource, serve_stream
from app.plugins.ssh.manager import SSHSessionManager
from app.plugins.ssh.routes import get_ssh_manager
from app.plugins.ssh.schemas import SESSION_ID_PATTERN

router = APIRouter(tags=["logs"])

DEFAULT_DIRECTORY = "/var/log"
MAX_UNITS = 20

Manager = Annotated[SSHSessionManager, Depends(get_ssh_manager)]
Server = Annotated[
    str, Path(pattern=SERVER_PATTERN, description='Сервер "host:port", как в метриках')
]
SessionQuery = Annotated[
    str | None,
    Query(pattern=SESSION_ID_PATTERN, description="Конкретная сессия к этому серверу"),
]
Lines = Annotated[int, Query(ge=0, description="Сколько последних строк (до LOGS_MAX_LINES)")]
Grep = Annotated[
    str | None,
    Query(min_length=1, max_length=GREP_MAX, description="Текст в строке (не регулярка)"),
]
FilePath = Annotated[str, Query(pattern=PATH_PATTERN, description="Абсолютный путь на сервере")]

ERRORS = {
    403: {"description": "Пользователь SSH не может читать файл (log_file_permission_denied)"},
    404: {
        "description": (
            "Нет открытой сессии к серверу (logs_server_not_connected), journalctl "
            "(logs_journal_unavailable) или файла (log_file_not_found)"
        )
    },
    409: {"description": "Сессия занята командой пользователя дольше SSH_SERVICE_WAIT"},
    422: {"description": "Неверный фильтр журнала (logs_bad_filter) или путь — папка"},
    502: {"description": "journalctl или чтение файла завершились с ошибкой (текст в detail)"},
    504: {"description": "Команда не уложилась в LOGS_TIMEOUT"},
}


def _lines(lines: int) -> int:
    return min(lines, settings.LOGS_MAX_LINES)


def _units(units: list[str] | None) -> tuple[str, ...]:
    units = units or []
    if len(units) > MAX_UNITS:
        raise LogsBadFilterError(f"At most {MAX_UNITS} units at once")
    for unit in units:
        if not re.fullmatch(UNIT_PATTERN, unit):
            raise LogsBadFilterError(f"Not a systemd unit name: {unit!r}")
    return tuple(units)


def journal_query(
    unit: Annotated[
        list[str] | None,
        Query(description="Юнит systemd (можно несколько и шаблон: nginx*)"),
    ] = None,
    priority: Annotated[
        Level | None, Query(description="Этот уровень и важнее: err = emerg ... err")
    ] = None,
    severity: Annotated[
        Severity | None,
        Query(description="Вкладка UI: error (emerg..err), warn, info (notice, info), debug"),
    ] = None,
    grep: Grep = None,
    since: Annotated[
        str | None,
        Query(pattern=TIME_PATTERN, description="С какого времени: мс Unix, today, -1h, ..."),
    ] = None,
    until: Annotated[
        str | None, Query(pattern=TIME_PATTERN, description="До какого времени")
    ] = None,
    boot: Annotated[bool, Query(description="Только с последней загрузки сервера")] = False,
) -> journal.JournalQuery:
    if priority is not None and severity is not None:
        raise LogsBadFilterError("Pass either priority or severity, not both")
    return journal.JournalQuery(
        units=_units(unit),
        priority=priority_number(priority),
        severity=severity,
        grep=grep,
        since=since,
        until=until,
        boot=boot,
    )


Filters = Annotated[journal.JournalQuery, Depends(journal_query)]


@router.get(
    "/servers/{server}/sources",
    response_model=LogSources,
    summary="Что из логов сервера доступно: журнал systemd и файлы в /var/log",
    responses={404: ERRORS[404], 409: ERRORS[409], 504: ERRORS[504]},
)
async def sources(server: Server, manager: Manager, session_id: SessionQuery = None) -> LogSources:
    runner = service.LogRunner(manager, server, session_id)
    probe = await service.journal_probe(runner)
    files_detail = None
    try:
        files = await service.list_files(runner, DEFAULT_DIRECTORY)
    except service.GONE:
        raise
    except AppError as e:
        # Журнал уже есть в ответе: без списка файлов UI всё равно может работать
        files, files_detail = [], e.detail
    return LogSources(
        server=server,
        session_id=runner.session_id,
        journal=JournalInfo(
            available=probe.available,
            version=probe.version,
            access=probe.access,  # type: ignore[arg-type]
            detail=probe.detail,
        ),
        directory=DEFAULT_DIRECTORY,
        files=[LogFileInfo.of(item) for item in files],
        files_detail=files_detail,
    )


@router.get(
    "/servers/{server}/journal",
    response_model=JournalPage,
    summary="Записи журнала systemd: последние или страница старше",
    description=(
        "Без before: последние `lines` записей по фильтрам. С before (курсор самой старой "
        "из уже показанных): `lines` записей старше неё. Записи старые первыми."
    ),
    responses=ERRORS,
)
async def journal_entries(
    server: Server,
    manager: Manager,
    query: Filters,
    session_id: SessionQuery = None,
    lines: Lines = 200,
    before: Annotated[str | None, Query(pattern=CURSOR_PATTERN)] = None,
) -> JournalPage:
    runner = service.LogRunner(manager, server, session_id)
    count = _lines(lines)
    entries = await service.journal_page(runner, query, count, before) if count else []
    return JournalPage(
        server=server,
        session_id=runner.session_id,
        entries=[JournalEntryInfo.of(entry) for entry in entries],
        before=entries[0].cursor if entries else None,
        cursor=entries[-1].cursor if entries else None,
        more=count > 0 and len(entries) >= count,
    )


@router.get(
    "/servers/{server}/journal/units",
    response_model=JournalUnits,
    summary="Юниты systemd, которые писали в журнал (для фильтра)",
    responses={404: ERRORS[404], 409: ERRORS[409], 502: ERRORS[502], 504: ERRORS[504]},
)
async def journal_units(
    server: Server, manager: Manager, session_id: SessionQuery = None
) -> JournalUnits:
    runner = service.LogRunner(manager, server, session_id)
    units = await service.journal_units(runner)
    return JournalUnits(server=server, session_id=runner.session_id, units=units)


@router.websocket("/servers/{server}/journal/ws")
async def journal_stream(
    websocket: WebSocket,
    server: Server,
    manager: Manager,
    query: Filters,
    session_id: SessionQuery = None,
    lines: Lines = 200,
    after: Annotated[str | None, Query(pattern=CURSOR_PATTERN)] = None,
) -> None:
    """Живой журнал: последние `lines` записей и новые по мере появления. С after
    (cursor из прошлого подключения) продолжает без повторов. Протокол в
    app/plugins/logs/stream.py."""
    count = _lines(lines)
    source = JournalSource(query, count, after)
    await serve_stream(
        websocket,
        lambda: service.LogRunner(manager, server, session_id, companion=True, wait=_wait()),
        source,
        server,
        count,
    )


@router.get(
    "/servers/{server}/files",
    response_model=LogFiles,
    summary="Файлы логов в папке сервера и её подпапках",
    description="Сжатые (.gz, .xz) и двоичные (wtmp, btmp, lastlog, журнал) не показываются.",
    responses=ERRORS,
)
async def log_files(
    server: Server,
    manager: Manager,
    session_id: SessionQuery = None,
    directory: Annotated[str, Query(alias="dir", pattern=PATH_PATTERN)] = DEFAULT_DIRECTORY,
) -> LogFiles:
    runner = service.LogRunner(manager, server, session_id)
    files = await service.list_files(runner, directory)
    return LogFiles(
        server=server,
        session_id=runner.session_id,
        directory=directory,
        files=[LogFileInfo.of(item) for item in files],
    )


@router.get(
    "/servers/{server}/file",
    response_model=FileTail,
    summary="Последние строки файла лога",
    description="С grep: последние `lines` строк, в которых есть этот текст.",
    responses=ERRORS,
)
async def file_tail(
    server: Server,
    manager: Manager,
    path: FilePath,
    session_id: SessionQuery = None,
    lines: Lines = 200,
    grep: Grep = None,
) -> FileTail:
    runner = service.LogRunner(manager, server, session_id)
    count = _lines(lines)
    chunk = await service.read_tail(runner, path, count, grep)
    return FileTail(
        server=server,
        session_id=runner.session_id,
        path=path,
        lines=chunk.lines,
        size=chunk.size,
        offset=chunk.offset,
        inode=chunk.inode,
        truncated=not grep and chunk.skipped > 0 and len(chunk.lines) < count,
    )


@router.websocket("/servers/{server}/file/ws")
async def file_stream(
    websocket: WebSocket,
    server: Server,
    manager: Manager,
    path: FilePath,
    session_id: SessionQuery = None,
    lines: Lines = 200,
    grep: Grep = None,
    offset: Annotated[int | None, Query(ge=0, description="offset из прошлого подключения")] = None,
    inode: Annotated[str | None, Query(pattern=r"^\d{1,24}$")] = None,
) -> None:
    """Живой файл лога: последние `lines` строк и новые по мере записи. С offset и inode
    продолжает с того же места. Протокол в app/plugins/logs/stream.py."""
    count = _lines(lines)
    source = FileSource(path, count, grep, offset, inode)
    await serve_stream(
        websocket,
        lambda: service.LogRunner(manager, server, session_id, companion=True, wait=_wait()),
        source,
        server,
        count,
    )


def _wait() -> float:
    # Занятую сессию (без служебной) ждём не дольше периода опроса: следующий тик снова
    return min(settings.LOGS_INTERVAL, settings.SSH_SERVICE_WAIT)
