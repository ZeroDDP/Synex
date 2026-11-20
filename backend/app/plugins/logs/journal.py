"""Журнал systemd на сервере: команды journalctl и разбор его ответа.

Каждая команда здесь — один вызов journalctl без конвейеров: так её выполняет и эмулятор
shell в тестах (tests/fake_journal.py).

Особенности journalctl, под которые подобраны аргументы (проверено на systemd 255):

* записи читаются с конца (-r): последние N (-n N) и страница старше курсора
  (-r --after-cursor=C -n N) возвращаются правильно и с шаблоном (-g);
* вперёд от курсора (живой лог) -n с -g не работает: journalctl идёт назад от курсора.
  Поэтому с шаблоном новые записи берутся без -n (за период опроса их немного), без
  шаблона с -n (ограничение на случай переподключения со старым курсором);
* -g без заглавных букв ищет без учёта регистра (латиница). Шаблон экранируется: UI
  ищет текст, а не регулярное выражение;
* --output-fields (systemd 236+) сокращает ответ, на старых версиях повторяем без него;
* --all: длинные сообщения не заменяются на null.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from typing import Any

LEVELS = ("emerg", "alert", "crit", "err", "warning", "notice", "info", "debug")
FIELDS = (
    "MESSAGE",
    "PRIORITY",
    "_SYSTEMD_UNIT",
    "_SYSTEMD_USER_UNIT",
    "UNIT",
    "SYSLOG_IDENTIFIER",
    "_COMM",
    "_PID",
    "_HOSTNAME",
    "_BOOT_ID",
)
MAX_MESSAGE = 16 * 1024  # длиннее сообщение обрезается (символов)

# Как journalctl отвечает, если чего-то не умеет: тогда повторяем без этого
_NO_OUTPUT_FIELDS = re.compile(r"output-fields", re.IGNORECASE)
_NO_GREP = re.compile(
    r"pattern matching support|unrecognized option.*grep|invalid option -- 'g'", re.IGNORECASE
)
_UNKNOWN_OPTION = re.compile(r"unrecognized option|invalid option|unknown option", re.IGNORECASE)


@dataclass(frozen=True)
class JournalQuery:
    """Фильтры журнала. Все значения уже проверены схемой (schemas.py)."""

    units: tuple[str, ...] = ()
    priority: int | None = None  # 0 (emerg) ... 7 (debug): этот уровень и важнее
    grep: str | None = None
    since: str | None = None
    until: str | None = None
    boot: bool = False  # только текущая загрузка


@dataclass(frozen=True)
class JournalEntry:
    cursor: str
    time: int  # мс Unix
    priority: int | None
    unit: str | None
    identifier: str | None
    pid: int | None
    hostname: str | None
    boot_id: str | None
    message: str

    @property
    def level(self) -> str | None:
        return LEVELS[self.priority] if self.priority is not None else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "cursor": self.cursor,
            "time": self.time,
            "priority": self.priority,
            "level": self.level,
            "unit": self.unit,
            "identifier": self.identifier,
            "pid": self.pid,
            "hostname": self.hostname,
            "boot_id": self.boot_id,
            "message": self.message,
        }


def grep_pattern(text: str) -> str:
    """Текст поиска -> шаблон для journalctl -g: спецсимволы экранированы, латиница в
    нижнем регистре (тогда journalctl ищет без учёта регистра)."""
    lowered = "".join(ch.lower() if ch.isascii() else ch for ch in text)
    return re.escape(lowered)


def time_arg(value: str) -> str:
    """since/until из API: Unix-время в мс -> @секунды; остальное (2026-11-20 10:00:00,
    today, -1h) journalctl понимает сам."""
    if value.isdigit():
        ms = int(value)
        return f"@{ms // 1000}.{ms % 1000:03d}"
    return value


def _filters(query: JournalQuery, *, grep: bool) -> list[str]:
    args: list[str] = []
    for unit in query.units:
        args += ["-u", unit]
    if query.priority is not None:
        args += ["-p", str(query.priority)]
    if query.boot:
        args.append("-b")
    if query.since:
        args += ["-S", time_arg(query.since)]
    if query.until:
        args += ["-U", time_arg(query.until)]
    if grep and query.grep:
        args += ["-g", grep_pattern(query.grep)]
    return args


def _base(output_fields: bool) -> list[str]:
    args = ["journalctl", "--no-pager", "-q", "-o", "json", "--all"]
    if output_fields:
        args.append("--output-fields=" + ",".join(FIELDS))
    return args


def page_command(
    query: JournalQuery,
    lines: int,
    before: str | None = None,
    *,
    output_fields: bool = True,
    grep: bool = True,
) -> str:
    """Последние `lines` записей (новые первыми) или страница старше курсора before."""
    args = [*_base(output_fields), *_filters(query, grep=grep), "-r", "-n", str(lines)]
    if before:
        args.append(f"--after-cursor={before}")
    return _join(args)


def follow_command(
    query: JournalQuery,
    after: str,
    limit: int,
    *,
    output_fields: bool = True,
    grep: bool = True,
) -> str:
    """Записи после курсора after, старые первыми."""
    args = [*_base(output_fields), *_filters(query, grep=grep), f"--after-cursor={after}"]
    if not (grep and query.grep):
        args += ["-n", str(limit)]
    return _join(args)


def tail_cursor_command() -> str:
    """Курсор последней записи журнала: с него живой лог начинает, когда старых записей
    по фильтрам нет или они не нужны (lines=0). Фильтры применяются уже к записям после
    курсора."""
    return "journalctl --no-pager -n 0 --show-cursor"


def units_command() -> str:
    """Юниты, которые что-то писали в журнал."""
    return _join(["journalctl", "--no-pager", "-F", "_SYSTEMD_UNIT"])


def probe_command() -> str:
    """Есть ли journalctl и что из журнала видно пользователю SSH (подсказки в stderr)."""
    return "journalctl --version; journalctl --no-pager -n 0"


def _join(args: list[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in args)


# ------------------------------------------------------------------- parsing


def parse_entries(stdout: str, *, newest_first: bool = False) -> list[JournalEntry]:
    """Строки JSON journalctl -> записи, старые первыми. Строки, которые не разобрать,
    пропускаются."""
    entries = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        entry = _entry(data)
        if entry is not None:
            entries.append(entry)
    if newest_first:
        entries.reverse()
    return entries


def _entry(data: dict[str, Any]) -> JournalEntry | None:
    cursor = data.get("__CURSOR")
    realtime = _int(data.get("__REALTIME_TIMESTAMP"))
    if not isinstance(cursor, str) or realtime is None:
        return None
    priority = _int(data.get("PRIORITY"))
    if priority is not None and not 0 <= priority <= 7:
        priority = None
    message = _text(data.get("MESSAGE"))
    if len(message) > MAX_MESSAGE:
        message = message[:MAX_MESSAGE] + "…"
    return JournalEntry(
        cursor=cursor,
        time=realtime // 1000,
        priority=priority,
        unit=_text(data.get("_SYSTEMD_UNIT") or data.get("_SYSTEMD_USER_UNIT") or data.get("UNIT"))
        or None,
        identifier=_text(data.get("SYSLOG_IDENTIFIER") or data.get("_COMM")) or None,
        pid=_int(data.get("_PID")),
        hostname=_text(data.get("_HOSTNAME")) or None,
        boot_id=_text(data.get("_BOOT_ID")) or None,
        message=message,
    )


def _text(value: Any) -> str:
    """Поле журнала: строка; список байтов (не UTF-8 или с управляющими символами);
    список значений, если поле повторяется в записи (берём первое)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value:
        if all(isinstance(item, int) for item in value):
            return bytes(item & 0xFF for item in value).decode("utf-8", "replace")
        return _text(value[0])
    return str(value)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_cursor(stdout: str) -> str | None:
    """Курсор из вывода --show-cursor ("-- cursor: s=...")."""
    for line in stdout.splitlines():
        if line.startswith("-- cursor: "):
            return line.removeprefix("-- cursor: ").strip() or None
    return None


def parse_units(stdout: str) -> list[str]:
    return sorted({line.strip() for line in stdout.splitlines() if line.strip()})


@dataclass(frozen=True)
class JournalProbe:
    available: bool
    version: str | None
    access: str  # all | user | none
    detail: str | None


def parse_probe(stdout: str, stderr: str, exit_code: int) -> JournalProbe:
    """Ответ probe_command: версия systemd и подсказки journalctl о правах."""
    match = re.search(r"^systemd (\d+)", stdout, re.MULTILINE)
    if match is None:
        detail = stderr.strip() or f"journalctl exited with {exit_code}"
        return JournalProbe(False, None, "none", f"journalctl is not available: {detail}")
    version = match.group(1)
    lower = stderr.lower()
    if "insufficient permissions" in lower:
        return JournalProbe(
            True,
            version,
            "none",
            "The SSH user cannot read the journal: add it to the systemd-journal or adm group",
        )
    if "not seeing messages from other users" in lower:
        return JournalProbe(
            True,
            version,
            "user",
            "Only the SSH user's own messages are visible: add it to the systemd-journal or "
            "adm group to see the system journal",
        )
    if "no journal files were found" in lower:
        return JournalProbe(
            True, version, "all", "The journal is empty: systemd-journald does not run here"
        )
    return JournalProbe(True, version, "all", None)


def lacks_output_fields(stderr: str) -> bool:
    return bool(_NO_OUTPUT_FIELDS.search(stderr) and _UNKNOWN_OPTION.search(stderr))


def lacks_grep(stderr: str) -> bool:
    return bool(_NO_GREP.search(stderr))


def matches(entry: JournalEntry, text: str) -> bool:
    """Поиск без -g (старый journalctl): подстрока в сообщении без учёта регистра."""
    return text.casefold() in entry.message.casefold()
