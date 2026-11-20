"""Поиск файлов вкладки Files вглубь папок.

Фильтр по имени в уже открытой папке UI делает сам: список у него есть. Здесь поиск
вглубь — по папке и её подпапкам, чего UI без backend не сделает.

Что ищется:
* name: текст в имени без учёта регистра ("nginx" найдёт nginx.conf и NGINX.log) или
  шаблон, если в нём есть * ? [ ("*.log", "access.log.[0-9]");
* contains: текст внутри файла (без учёта регистра латиницы); двоичные файлы и файлы
  больше SFTP_SEARCH_MAX_FILE пропускаются;
* type: file, dir или any; max_depth: сколько уровней вглубь; hidden: заходить ли в
  скрытые папки (.git, .cache) и показывать скрытые файлы.

На этом компьютере — обход папок в ширину. Поиск останавливается через
SFTP_SEARCH_TIMEOUT или после SFTP_SEARCH_LIMIT найденных: тогда UI получает то, что
успели найти, и timed_out или truncated.
"""

from __future__ import annotations

import os
import stat as statmod
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal

from app.core.config import settings
from app.plugins.sftp import local
from app.plugins.sftp.errors import (
    SftpBadSearchError,
    SftpLocalNotADirectoryError,
    SftpLocalNotFoundError,
)

# Часы поиска по этому компьютеру (тест подменяет их: на Windows time.monotonic тикает
# раз в 15 мс, и короткий поиск успевает закончиться за один тик)
_clock = time.monotonic

SearchType = Literal["any", "file", "dir"]
EntryType = Literal["file", "dir", "link", "other"]

GLOB_CHARS = "*?["
BINARY_PROBE = 8192


@dataclass(frozen=True)
class SearchQuery:
    name: str | None = None
    contains: str | None = None
    type: SearchType = "any"
    max_depth: int = 10
    hidden: bool = False
    limit: int = 1000

    @property
    def pattern(self) -> str | None:
        """Шаблон имени для find -iname / fnmatch."""
        if not self.name:
            return None
        if any(ch in self.name for ch in GLOB_CHARS):
            return self.name
        return f"*{self.name}*"


@dataclass(frozen=True)
class Found:
    path: str
    name: str
    parent: str | None
    type: EntryType | None  # None: сервер без GNU find не сообщает тип
    size: int | None
    modified_at: datetime | None

    @property
    def is_dir(self) -> bool:
        return self.type == "dir"


@dataclass
class SearchResult:
    path: str
    entries: list[Found] = field(default_factory=list)
    truncated: bool = False  # нашлось больше limit
    timed_out: bool = False  # остановлен по времени, найдено не всё
    denied: int = 0  # папок, в которые нет прав заходить
    elapsed_ms: int = 0


def check(query: SearchQuery) -> None:
    if not query.name and not query.contains:
        raise SftpBadSearchError("Say what to look for: a name (name) or text in files (contains)")
    if query.name and ("/" in query.name or "\\" in query.name):
        raise SftpBadSearchError("name is a file name or pattern, without folders")
    for value in (query.name, query.contains):
        if value and any(ord(ch) < 32 for ch in value):
            raise SftpBadSearchError("Search text must not contain control characters")


# ------------------------------------------------------------ this computer


def search_local(directory: str | None, query: SearchQuery) -> SearchResult:
    """Поиск на этом компьютере (вызывать в потоке: обход диска блокирует)."""
    check(query)
    root = local.resolve(directory)
    if not root.exists():
        raise SftpLocalNotFoundError(f"Folder {root} not found")
    if not root.is_dir():
        raise SftpLocalNotADirectoryError(f"{root} is not a folder")
    started = _clock()
    deadline = started + settings.SFTP_SEARCH_TIMEOUT
    result = SearchResult(path=str(root))
    pattern = query.pattern
    for item in _walk(root, query, result, deadline):
        kind = _kind(item)
        if query.type == "file" and kind != "file":
            continue
        if query.type == "dir" and kind != "dir":
            continue
        if pattern and not _match(item.name, pattern):
            continue
        if query.contains and (kind != "file" or not _contains(item, query.contains)):
            continue
        result.entries.append(_found(item, kind))
        if len(result.entries) > query.limit:
            result.entries.pop()
            result.truncated = True
            break
        if _clock() > deadline:
            result.timed_out = True
            break
    result.elapsed_ms = round((_clock() - started) * 1000)
    return result


def _walk(
    root: Path, query: SearchQuery, result: SearchResult, deadline: float
) -> Iterator[os.DirEntry[str]]:
    """Обход в ширину: сначала ближние уровни (так найденное ближе к папке поиска).
    Ссылки на папки не открываются: иначе возможны петли."""
    level: list[str] = [str(root)]
    for _ in range(query.max_depth):
        following: list[str] = []
        for directory in level:
            if _clock() > deadline:
                result.timed_out = True
                return
            try:
                items = sorted(os.scandir(directory), key=lambda e: e.name.casefold())
            except PermissionError:
                result.denied += 1
                continue
            except OSError:
                continue
            for item in items:
                if not query.hidden and _hidden(item):
                    continue
                yield item
                if result.truncated or result.timed_out:
                    return
                try:
                    if item.is_dir(follow_symlinks=False):
                        following.append(item.path)
                except OSError:
                    continue
        if not following:
            return
        level = following


def _hidden(item: os.DirEntry[str]) -> bool:
    if item.name.startswith("."):
        return True
    try:
        attributes = getattr(item.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(statmod, "FILE_ATTRIBUTE_HIDDEN", 0))


def _kind(item: os.DirEntry[str]) -> EntryType:
    try:
        if item.is_symlink():
            return "link"
        if item.is_dir(follow_symlinks=False):
            return "dir"
        if item.is_file(follow_symlinks=False):
            return "file"
    except OSError:
        pass
    return "other"


def _match(name: str, pattern: str) -> bool:
    return fnmatchcase(name.casefold(), pattern.casefold())


def _contains(item: os.DirEntry[str], text: str) -> bool:
    """Есть ли text в файле (без учёта регистра). Двоичные и большие пропускаются."""
    try:
        if item.stat().st_size > settings.SFTP_SEARCH_MAX_FILE:
            return False
        with open(item.path, "rb") as file:
            data = file.read(settings.SFTP_SEARCH_MAX_FILE)
    except OSError:
        return False
    if b"\0" in data[:BINARY_PROBE]:
        return False
    return text.casefold() in data.decode("utf-8", "ignore").casefold()


def _found(item: os.DirEntry[str], kind: EntryType) -> Found:
    try:
        info = item.stat(follow_symlinks=False)
    except OSError:
        info = None
    parent = os.path.dirname(item.path)
    return Found(
        path=item.path,
        name=item.name,
        parent=parent or None,
        type=kind,
        size=info.st_size if info is not None and kind != "dir" else None,
        modified_at=datetime.fromtimestamp(info.st_mtime, UTC) if info is not None else None,
    )
