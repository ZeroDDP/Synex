from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.plugins.logs.files import LogFile
from app.plugins.logs.journal import LEVELS, JournalEntry

# Сервер как в метриках и алертах: "host:port" (ServerMetrics.server)
SERVER_PATTERN = r"^[^\s/]{1,300}:\d{1,5}$"
# Юнит systemd или шаблон (journalctl -u "nginx*"); не начинается с "-": не станет флагом
UNIT_PATTERN = r"^[A-Za-z0-9@_.:*?\[\]\\][A-Za-z0-9@_.:*?\[\]\\-]{0,255}$"
# since/until: Unix-время в мс или то, что понимает journalctl (2026-11-20 10:00:00,
# today, yesterday, -1h, -30min)
TIME_PATTERN = r"^[0-9A-Za-z][0-9A-Za-z :.+\-]{0,63}$|^-[0-9]{1,6}[a-z]{1,7}$"
# Курсор журнала (__CURSOR): s=...;i=...;b=...;m=...;t=...;x=...
CURSOR_PATTERN = r"^[A-Za-z0-9=;_-]{1,512}$"
# Абсолютный путь на сервере
PATH_PATTERN = r"^/[^\x00\n\r]{0,4095}$"
GREP_MAX = 200

Level = Literal["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"]
# Вкладки уровня в UI: error = emerg..err, warn = warning, info = notice и info, debug
Severity = Literal["error", "warn", "info", "debug"]


def priority_number(level: Level | None) -> int | None:
    return LEVELS.index(level) if level is not None else None


class JournalEntryInfo(BaseModel):
    cursor: str = Field(description="Курсор записи: для страницы старше (before) и after")
    time: int = Field(description="Время записи, мс Unix")
    priority: int | None = Field(description="0 (emerg) ... 7 (debug)")
    level: Level | None
    severity: Severity = Field(description="Вкладка UI: error, warn, info или debug")
    unit: str | None = Field(description="Юнит systemd, например nginx.service")
    identifier: str | None = Field(description="Программа (SYSLOG_IDENTIFIER или имя процесса)")
    pid: int | None
    hostname: str | None
    boot_id: str | None
    message: str

    @classmethod
    def of(cls, entry: JournalEntry) -> JournalEntryInfo:
        return cls(**entry.as_dict())


class JournalPage(BaseModel):
    server: str
    session_id: str
    entries: list[JournalEntryInfo] = Field(description="Старые первыми")
    before: str | None = Field(
        description="Курсор самой старой записи: ?before= отдаст страницу старше"
    )
    cursor: str | None = Field(
        description="Курсор самой новой записи: живой лог с ним (?after=) продолжит отсюда"
    )
    more: bool = Field(description="Записей было не меньше lines: есть смысл просить старше")


class JournalInfo(BaseModel):
    available: bool = Field(description="journalctl есть (сервер на systemd)")
    version: str | None = Field(description="Версия systemd")
    access: Literal["all", "user", "none"] = Field(
        description=(
            "all: виден весь журнал; user: только сообщения пользователя SSH (нет в группе "
            "systemd-journal или adm); none: журнал не прочитать"
        )
    )
    detail: str | None = Field(description="Почему журнал не виден целиком или пуст")


class LogFileInfo(BaseModel):
    path: str
    name: str
    readable: bool = Field(description="Пользователь SSH может его читать")
    size: int | None = Field(description="Байт")
    modified: int | None = Field(description="Когда менялся, мс Unix")
    rotated: bool = Field(description="Старая копия после logrotate (syslog.1)")

    @classmethod
    def of(cls, item: LogFile) -> LogFileInfo:
        return cls(
            path=item.path,
            name=item.path.rsplit("/", 1)[-1],
            readable=item.readable,
            size=item.size,
            modified=item.modified,
            rotated=item.rotated,
        )


class LogFiles(BaseModel):
    server: str
    session_id: str
    directory: str
    files: list[LogFileInfo]


class LogSources(BaseModel):
    server: str
    session_id: str
    journal: JournalInfo
    directory: str = Field(description="Папка, из которой взяты files")
    files: list[LogFileInfo] = Field(description="Файлы логов; [] если папки нет")
    files_detail: str | None = Field(
        default=None, description="Почему список файлов пуст: папки нет или её не прочитать"
    )


class JournalUnits(BaseModel):
    server: str
    session_id: str
    units: list[str] = Field(description="Юниты, которые писали в журнал")


class FileTail(BaseModel):
    server: str
    session_id: str
    path: str
    lines: list[str] = Field(description="Старые первыми")
    size: int = Field(description="Размер файла на момент чтения, байт")
    offset: int = Field(description="С какого байта живой лог (?offset=) продолжит")
    inode: str | None = Field(description="inode файла: с ним живой лог заметит logrotate")
    truncated: bool = Field(
        description="Строк меньше lines, потому что в окно LOGS_CHUNK_BYTES больше не влезло"
    )
