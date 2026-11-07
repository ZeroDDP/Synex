"""Команды git на сервере и разбор их вывода.

Вывод для разбора берётся из машиночитаемых форматов: `git status --porcelain=v2
--branch` и `git log --format` с разделителем 0x1F. Сообщения git на английском
(LC_ALL=C): по ним распознаются ошибки.
"""

from __future__ import annotations

import posixpath
import shlex
from dataclasses import dataclass, field
from datetime import datetime

# Переменные перед каждой командой git: английские сообщения; без вопросов логина и
# пароля в терминал (их всё равно некому ввести: stdin команды /dev/null)
GIT_ENV = "LC_ALL=C GIT_TERMINAL_PROMPT=0"
FIELD_SEPARATOR = "\x1f"
LOG_FORMAT = "%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%s"


@dataclass(frozen=True)
class Commit:
    hash: str
    short_hash: str
    author_name: str
    author_email: str
    date: datetime | None
    subject: str


@dataclass(frozen=True)
class FileChange:
    path: str
    kind: str  # changed, renamed, conflict, untracked
    index: str  # состояние в индексе: M, A, D, R, C, T, U или "." (без изменений)
    worktree: str  # состояние в рабочей папке, те же буквы
    orig_path: str | None = None  # откуда переименован


@dataclass
class RepoStatus:
    head: str | None = None  # None: в репозитории ещё нет коммитов
    branch: str | None = None  # None: detached HEAD
    upstream: str | None = None
    ahead: int | None = None
    behind: int | None = None
    changes: list[FileChange] = field(default_factory=list)


def git_command(path: str, *args: str) -> str:
    """`LC_ALL=C ... git -C <path> <args>` для shell сервера."""
    quoted = " ".join(shlex.quote(arg) for arg in args)
    return f"{GIT_ENV} git -C {quote_path(path)} {quoted}"


def quote_path(path: str) -> str:
    """Путь для shell сервера: `~` и `~/...` раскрываются в домашнюю папку, остальное
    в кавычках. Относительный путь считается от домашней папки (служебные команды
    выполняются оттуда)."""
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        rest = posixpath.normpath(path[2:])
        return '"$HOME"' if rest == "." else f'"$HOME"/{shlex.quote(rest)}'
    return shlex.quote(path)


def status_args() -> list[str]:
    return ["-c", "core.quotePath=false", "status", "--porcelain=v2", "--branch"]


def log_args(limit: int, revisions: str | None = None) -> list[str]:
    args = ["log", f"--max-count={limit}", f"--format={LOG_FORMAT}"]
    if revisions:
        args.append(revisions)
    return args


def parse_status(stdout: str) -> RepoStatus:
    status = RepoStatus()
    for line in stdout.splitlines():
        if line.startswith("# "):
            key, _, value = line[2:].partition(" ")
            if key == "branch.oid":
                status.head = None if value == "(initial)" else value
            elif key == "branch.head":
                status.branch = None if value == "(detached)" else value
            elif key == "branch.upstream":
                status.upstream = value
            elif key == "branch.ab":
                ahead, _, behind = value.partition(" ")
                status.ahead = _int(ahead.lstrip("+"))
                status.behind = _int(behind.lstrip("-"))
        elif line.startswith("1 "):
            parts = line.split(" ", 8)
            if len(parts) == 9:
                xy = parts[1]
                status.changes.append(FileChange(unquote(parts[8]), "changed", xy[0], xy[1]))
        elif line.startswith("2 "):
            parts = line.split(" ", 9)
            if len(parts) == 10:
                xy = parts[1]
                target, _, source = parts[9].partition("\t")
                status.changes.append(
                    FileChange(unquote(target), "renamed", xy[0], xy[1], unquote(source))
                )
        elif line.startswith("u "):
            parts = line.split(" ", 10)
            if len(parts) == 11:
                xy = parts[1]
                status.changes.append(FileChange(unquote(parts[10]), "conflict", xy[0], xy[1]))
        elif line.startswith("? "):
            status.changes.append(FileChange(unquote(line[2:]), "untracked", "?", "?"))
    return status


def parse_log(stdout: str) -> list[Commit]:
    commits = []
    for line in stdout.splitlines():
        parts = line.split(FIELD_SEPARATOR)
        if len(parts) != 6 or not parts[0]:
            continue
        full, short, name, email, date, subject = parts
        commits.append(Commit(full, short, name, email, _date(date), subject))
    return commits


def unquote(path: str) -> str:
    """Путь из вывода git: в кавычках со стилем C ("a\\"b"), если в нём есть спецсимволы."""
    if len(path) < 2 or path[0] != '"' or path[-1] != '"':
        return path
    escapes = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "a": "\a", "b": "\b", "r": "\r"}
    raw = bytearray()
    chars = path[1:-1]
    index = 0
    while index < len(chars):
        char = chars[index]
        if char == "\\" and index + 1 < len(chars):
            following = chars[index + 1]
            octal = chars[index + 1 : index + 4]
            if len(octal) == 3 and all(c in "01234567" for c in octal):
                raw.append(int(octal, 8))  # байт UTF-8, записанный как \303\274
                index += 4
                continue
            raw.extend(escapes.get(following, following).encode())
            index += 2
            continue
        raw.extend(char.encode())
        index += 1
    return raw.decode("utf-8", errors="replace")


def _int(value: str) -> int | None:
    return int(value) if value.isdigit() else None


def _date(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
