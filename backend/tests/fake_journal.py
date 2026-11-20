"""Фейковый journalctl для эмулятора shell (tests/fake_shell.py): журнал в памяти.

Понимает то, что отправляет плагин logs (app/plugins/logs/journal.py), и отвечает как
systemd 255: -o json, --output-fields, --all, -q, -u (с шаблоном), -p, -b, -S, -U, -g
(без заглавных букв без учёта регистра), -r, -n, --after-cursor, --show-cursor, -F,
--version. Как настоящий: с курсором -n ограничивает количество от курсора, -r идёт к
старым записям.

old=True: journalctl старше 236 без PCRE2 (нет --output-fields и -g).
access: "all", "user" (подсказка «не видите сообщения других»), "none" (нет прав).
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

BOOT = "0123456789abcdef0123456789abcdef"
START_US = 1_795_000_000_000_000  # 2026-11-18


@dataclass
class FakeJournal:
    entries: list[dict[str, str]] = field(default_factory=list)
    old: bool = False
    access: str = "all"
    installed: bool = True
    calls: list[list[str]] = field(default_factory=list)

    def add(
        self,
        message: str,
        *,
        unit: str = "nginx.service",
        priority: int = 6,
        seconds: float | None = None,
        identifier: str | None = None,
    ) -> dict[str, str]:
        index = len(self.entries) + 1
        at = START_US + int((seconds if seconds is not None else index) * 1_000_000)
        entry = {
            "__CURSOR": f"s=fake;i={index:x};b={BOOT};t={at:x}",
            "__REALTIME_TIMESTAMP": str(at),
            "__MONOTONIC_TIMESTAMP": str(index),
            "_BOOT_ID": BOOT,
            "_HOSTNAME": "fake-host",
            "PRIORITY": str(priority),
            "_SYSTEMD_UNIT": unit,
            "SYSLOG_IDENTIFIER": identifier or unit.split(".")[0],
            "_PID": str(100 + index),
            "MESSAGE": message,
        }
        self.entries.append(entry)
        return entry

    def __call__(self, args: list[str], cwd: str, env: dict[str, str]) -> tuple[str, str, int]:
        self.calls.append(args)
        if not self.installed:
            return "", "sh: 1: journalctl: not found\n", 127
        try:
            return self._run(args)
        except _Error as e:
            return "", f"{e}\n", 1

    # -------------------------------------------------------------- options

    def _run(self, args: list[str]) -> tuple[str, str, int]:
        options = _Options()
        items = iter(args)
        for arg in items:
            if arg == "--version":
                features = "-PCRE2" if self.old else "+PCRE2"
                return f"systemd {219 if self.old else 255} (fake)\n{features} +LZ4\n", "", 0
            if arg in ("--no-pager", "--all", "-a"):
                continue
            if arg == "-q":
                options.quiet = True
            elif arg == "-o":
                options.output = next(items)
            elif arg.startswith("--output-fields="):
                if self.old:
                    raise _Error(f"journalctl: unrecognized option '{arg}'")
                options.fields = arg.split("=", 1)[1].split(",")
            elif arg == "-u":
                options.units.append(next(items))
            elif arg == "-p":
                value = next(items)
                low, _, high = value.partition("..")
                if not low.isdigit() or (high and not high.isdigit()):
                    raise _Error(f"Unknown log level {value}")
                options.priority = (int(low), int(high)) if high else (0, int(low))
            elif arg == "-b":
                options.boot = True
            elif arg in ("-S", "-U"):
                value = _timestamp(next(items))
                if arg == "-S":
                    options.since = value
                else:
                    options.until = value
            elif arg == "-g":
                if self.old:
                    raise _Error("Compiled without pattern matching support")
                pattern = next(items)
                flags = 0 if any(ch.isupper() for ch in pattern) else re.IGNORECASE
                try:
                    options.grep = re.compile(pattern, flags)
                except re.error as e:
                    raise _Error(f'Bad pattern "{pattern}": {e}') from None
            elif arg == "-r":
                options.reverse = True
            elif arg == "-n":
                options.lines = int(next(items))
            elif arg.startswith("--after-cursor="):
                options.after = arg.split("=", 1)[1]
            elif arg == "--show-cursor":
                options.show_cursor = True
            elif arg == "-F":
                field_name = next(items)
                values = {e[field_name] for e in self._visible() if field_name in e}
                return "".join(f"{value}\n" for value in values), "", 0
            else:
                raise _Error(f"journalctl: unrecognized option '{arg}'")
        return self._query(options)

    def _visible(self) -> list[dict[str, str]]:
        return [] if self.access == "none" else self.entries

    def _hints(self, quiet: bool) -> str:
        if quiet:
            return ""
        if self.access == "none":
            return "No journal files were opened due to insufficient permissions.\n"
        if self.access == "user":
            return (
                "Hint: You are currently not seeing messages from other users and the "
                "system.\n      Users in groups 'adm', 'systemd-journal' can see all "
                "messages.\n      Pass -q to turn off this notice.\n"
            )
        if not self.entries:
            return "No journal files were found.\n"
        return ""

    def _query(self, o: _Options) -> tuple[str, str, int]:
        entries = [e for e in self._visible() if o.matches(e)]
        cursors = [e["__CURSOR"] for e in self.entries]
        if o.after is not None:
            if o.after not in cursors:
                raise _Error("Failed to seek to cursor: Invalid argument")
            position = cursors.index(o.after)
            after = [e for e in entries if cursors.index(e["__CURSOR"]) > position]
            before = [e for e in entries if cursors.index(e["__CURSOR"]) < position]
            if o.reverse:
                selected = list(reversed(before))
                if o.lines is not None:
                    selected = selected[: o.lines]
            else:
                selected = after if o.lines is None else after[: o.lines]
        else:
            selected = entries if o.lines is None else entries[-o.lines :] if o.lines else []
            if o.reverse:
                selected = list(reversed(selected))
        stderr = self._hints(o.quiet)
        out = []
        for entry in selected:
            if o.output == "json":
                fields = o.fields
                data = {
                    k: v
                    for k, v in entry.items()
                    if fields is None or k in fields or k.startswith("__")
                }
                out.append(json.dumps(data, ensure_ascii=False))
            else:
                out.append(entry["MESSAGE"])
        if not selected and not o.quiet:
            out.append("-- No entries --")
        # Как настоящий: с -g и без находок код 1
        code = 1 if not selected and o.grep is not None else 0
        if o.show_cursor:
            last = selected[-1] if selected else (self._visible() or [None])[-1]
            if last is not None:
                out.append(f"-- cursor: {last['__CURSOR']}")
        return "".join(f"{line}\n" for line in out), stderr, code


class _Error(Exception):
    pass


@dataclass
class _Options:
    quiet: bool = False
    output: str = "short"
    fields: list[str] | None = None
    units: list[str] = field(default_factory=list)
    priority: tuple[int, int] | None = None  # от и до включительно
    boot: bool = False
    since: int | None = None
    until: int | None = None
    grep: re.Pattern[str] | None = None
    reverse: bool = False
    lines: int | None = None
    after: str | None = None
    show_cursor: bool = False

    def matches(self, entry: dict[str, str]) -> bool:
        at = int(entry["__REALTIME_TIMESTAMP"])
        if self.units and not any(
            fnmatch.fnmatchcase(entry.get("_SYSTEMD_UNIT", ""), unit)
            or fnmatch.fnmatchcase(entry.get("_SYSTEMD_UNIT", ""), f"{unit}.service")
            for unit in self.units
        ):
            return False
        if self.priority is not None:
            low, high = sorted(self.priority)
            if not low <= int(entry.get("PRIORITY", "6")) <= high:
                return False
        if self.since is not None and at < self.since:
            return False
        if self.until is not None and at > self.until:
            return False
        return not (self.grep is not None and not self.grep.search(entry["MESSAGE"]))


def _timestamp(value: str) -> int:
    """-S/-U: @секунды или "ГГГГ-ММ-ДД ЧЧ:ММ:СС" (UTC) -> мкс."""
    if value.startswith("@"):
        try:
            return int(float(value[1:]) * 1_000_000)
        except ValueError:
            pass
    else:
        try:
            moment = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            return int(moment.timestamp() * 1_000_000)
        except ValueError:
            pass
    raise _Error(f"Failed to parse timestamp: {value}")
