"""Терминал UI поверх команд сессии: защита от полноэкранных программ, история команд по
серверам и дополнение по Tab.

Команда терминала выполняется без TTY и без ввода (wrapper.py), поэтому полноэкранные
программы (vim, top, tmux) работать не могут: висят до таймаута или сразу падают, а их
escape-коды ломают экран xterm в UI. Такие команды backend не отправляет и подсказывает
замену (422 ssh_interactive_command); allow_interactive=true отправляет всё равно.

История (terminal_history.json рядом с keys.json) общая для всех сессий к одному серверу
(user@host:port) и переживает перезапуск. Команда с пробелом в начале не запоминается,
как в bash с HISTCONTROL=ignorespace: так вводят команды с паролями.

Дополнение — служебная команда сессии: compgen из bash (команды из PATH, файлы и папки
от текущей папки сессии), без bash — файлы через шаблон shell.
"""

from __future__ import annotations

import json
import re
import shlex
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import structlog

from app.core.config import settings
from app.core.files import write_private_file
from app.plugins.ssh.errors import SSHInteractiveCommandError
from app.plugins.ssh.manager import ExecResult, SSHSessionManager
from app.plugins.ssh.schemas import SSHCommandRequest

logger = structlog.get_logger()

# ------------------------------------------------------------ interactive


@dataclass(frozen=True)
class Interactive:
    program: str
    hint: str


_EDIT = "Edit files in the Files tab."
_STATS = "Use top -b -n 1 for one snapshot, or the Dashboard."
# Программы, которым нужен терминал: без него висят до таймаута или ломают экран UI
TTY_PROGRAMS: dict[str, str] = {
    **dict.fromkeys(("vi", "vim", "nvim", "view", "vimdiff", "nano", "pico"), _EDIT),
    **dict.fromkeys(("emacs", "mcedit", "joe", "micro", "ne"), _EDIT),
    **dict.fromkeys(("top", "htop", "btop", "atop", "glances"), _STATS),
    "iftop": "See the network chart on the Dashboard.",
    "nethogs": "Use nethogs -t -c 1.",
    "iotop": "Use iotop -b -n 1.",
    "watch": "Run the command again yourself; follow logs in the Logs tab.",
    **dict.fromkeys(("tmux", "screen"), "Synex sessions already stay open between commands."),
    **dict.fromkeys(("mc", "ranger", "nnn"), "Use the Files tab."),
    "ncdu": "Use du -sh * | sort -h.",
    "nmtui": "Use nmcli.",
    "alsamixer": "Use amixer.",
    "visudo": "Put a file into /etc/sudoers.d with the Files tab.",
}
# С этими аргументами программа работает без терминала
_BATCH: dict[str, re.Pattern[str]] = {
    "top": re.compile(r"(^|\s)-\w*b"),
    "atop": re.compile(r"(^|\s)\d"),
    "emacs": re.compile(r"--batch|-batch|--script"),
    "nethogs": re.compile(r"(^|\s)-\w*t"),
    "iotop": re.compile(r"(^|\s)-\w*b"),
}
# Команды, которые открывают редактор ($EDITOR): без терминала он висит до таймаута
_MESSAGE_FLAGS = re.compile(r"(^|\s)(-\w*[mFC]|--message|--file|--no-edit|--fixup|--reuse-message)")


def _opens_editor(name: str, args: list[str]) -> str | None:
    line = " ".join(args)
    sub = args[0] if args else ""
    if name == "git" and sub == "commit" and not _MESSAGE_FLAGS.search(line):
        return 'git commit -m "message"'
    if name == "git" and sub == "rebase" and re.search(r"(^|\s)(-i|--interactive)", line):
        return "git rebase without -i"
    if name == "git" and sub in ("merge", "tag") and re.search(r"(^|\s)(-e|--edit)", line):
        return f"git {sub} with -m"
    if name == "crontab" and re.search(r"(^|\s)-e", line):
        return "crontab FILE (prepare the file in the Files tab) or crontab -l"
    if name in ("systemctl", "kubectl") and sub == "edit":
        return f"{name} with a file prepared in the Files tab"
    return None


_PREFIXES = {"sudo", "doas", "exec", "nohup", "time", "command", "env", "nice", "ionice", "stdbuf"}
# Флаги префиксов, у которых есть значение (sudo -u user, nice -n 5)
_PREFIX_VALUE_FLAGS: dict[str, set[str]] = {
    "sudo": {"-u", "-g", "-C", "-h", "-p", "-r", "-t", "-U", "-D"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "-S"},
    "nice": {"-n"},
    "ionice": {"-c", "-n", "-p"},
    "stdbuf": {"-i", "-o", "-e"},
    "time": {"-f", "-o"},
}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_OPERATORS = set(";&|()\n")


def _commands(command: str) -> list[list[str]]:
    """Слова каждой простой команды: строка делится по ; && || | & и переводам строк,
    кавычки учитываются."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t"
    lexer.whitespace_split = True
    lexer.commenters = "#"
    commands: list[list[str]] = [[]]
    try:
        for token in lexer:
            if token and set(token) <= _OPERATORS:
                commands.append([])
            else:
                commands[-1].append(token)
    except ValueError:  # незакрытая кавычка: shell её тоже не выполнит
        return [command.split()]
    return [words for words in commands if words]


def interactive_program(command: str) -> Interactive | None:
    """Первая программа в команде (в каждой части конвейера и списка), которой нужен
    терминал."""
    for words in _commands(command):
        name, args = _program(words)
        if name is None:
            continue
        editor = _opens_editor(name, args)
        if editor is not None:
            return Interactive(program=name, hint=f"It opens an editor: use {editor}.")
        hint = TTY_PROGRAMS.get(name)
        if hint is None:
            continue
        batch = _BATCH.get(name)
        if batch is not None and batch.search(" ".join(args)):
            continue
        return Interactive(program=name, hint=hint)
    return None


def _program(words: list[str]) -> tuple[str | None, list[str]]:
    """Имя программы (без пути) и её аргументы: пропускаются VAR=value и префиксы вроде
    sudo -u user, env, nice -n 5."""
    index = 0
    while index < len(words):
        word = words[index]
        if _ASSIGNMENT.match(word):
            index += 1
            continue
        name = word.rsplit("/", 1)[-1]
        if name not in _PREFIXES:
            return name, words[index + 1 :]
        index += 1
        value_flags = _PREFIX_VALUE_FLAGS.get(name, set())
        while index < len(words) and words[index].startswith("-"):
            flag = words[index]
            index += 1
            if flag in value_flags and index < len(words):
                index += 1
    return None, []


# -------------------------------------------------------------------- history


def history_key(username: str, host: str, port: int) -> str:
    return f"{username}@{host.lower()}:{port}"


class TerminalHistory:
    """История команд по серверам в одном JSON-файле."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, list[str]] | None = None

    def _load(self) -> dict[str, list[str]]:
        if self._data is not None:
            return self._data
        data: dict[str, list[str]] = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            servers = raw.get("servers", {}) if isinstance(raw, dict) else {}
            for key, commands in servers.items():
                if isinstance(key, str) and isinstance(commands, list):
                    data[key] = [c for c in commands if isinstance(c, str)]
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            # Испорченная история не мешает работать: начинаем новую
            logger.warning("Terminal history is unreadable, starting anew", error=repr(e))
        self._data = data
        return data

    def _save(self) -> None:
        assert self._data is not None
        content = {"version": 1, "servers": self._data}
        try:
            write_private_file(self.path, json.dumps(content, ensure_ascii=False, indent=1))
        except OSError as e:
            logger.warning("Cannot save terminal history", error=repr(e))

    def add(self, key: str, command: str) -> bool:
        """Запомнить команду. False: не запоминается (пробел в начале, пустая, история
        выключена)."""
        limit = settings.TERMINAL_HISTORY
        text = command.rstrip("\n")
        if limit <= 0 or not text.strip() or text[:1].isspace():
            return False
        with self._lock:
            data = self._load()
            commands = data.setdefault(key, [])
            if commands and commands[-1] == text:
                return False
            commands.append(text)
            del commands[:-limit]
            self._save()
        return True

    def get(self, key: str, limit: int | None = None) -> list[str]:
        with self._lock:
            commands = list(self._load().get(key, []))
        return commands[-limit:] if limit else commands

    def clear(self, key: str) -> int:
        with self._lock:
            removed = self._load().pop(key, [])
            if removed:
                self._save()
        return len(removed)


_history: TerminalHistory | None = None


def get_history() -> TerminalHistory:
    global _history
    path = settings.terminal_history_path
    if _history is None or _history.path != path:
        _history = TerminalHistory(path)
    return _history


# ----------------------------------------------------------------- completion

CompletionKind = Literal["command", "path"]
MAX_CANDIDATES = 200
_WORD_BREAKS = " \t\n;|&<>()"


@dataclass(frozen=True)
class Word:
    text: str  # что дополняем (без кавычек)
    start: int  # где слово начинается в строке
    kind: CompletionKind


def current_word(line: str, cursor: int) -> Word:
    """Слово под курсором и что в нём ждать: команду (первое слово команды) или путь."""
    before = line[:cursor]
    start = len(before)
    while start > 0:
        if before[start - 1] == " " and start > 1 and before[start - 2] == "\\":
            start -= 2  # пробел в имени, экранированный "\ "
            continue
        if before[start - 1] in _WORD_BREAKS:
            break
        start -= 1
    raw = before[start:]
    text = raw.replace("\\ ", " ").strip("'\"")
    # Слова текущей команды до дополняемого: если там только sudo, env, VAR=value и
    # подобное, дополняется сама команда
    head = re.split(r"\|\||&&|[;|&(\n]", before[:start])[-1]
    first = all(_ASSIGNMENT.match(w) or w in _PREFIXES for w in head.split())
    kind: CompletionKind = "command" if first and "/" not in text else "path"
    return Word(text=text, start=start, kind=kind)


def completion_command(word: Word, cwd: str | None) -> str:
    """Скрипт дополнения: кандидаты по строке, у папок "/" в конце."""
    prefix = shlex.quote(word.text)
    go = f"cd -- {shlex.quote(cwd)} 2>/dev/null || cd\n" if cwd else ""
    limit = MAX_CANDIDATES + 1
    if word.kind == "command":
        bash = f"compgen -c -- {prefix} | sort -u | head -n {limit}"
        fallback = (
            'IFS=:; for d in $PATH; do for f in "$d"/' + _glob(word.text) + "; do "
            '[ -x "$f" ] && [ ! -d "$f" ] && printf "%s\\n" "${f##*/}"; done; done '
            f"| sort -u | head -n {limit}"
        )
    else:
        bash = (
            f"compgen -f -- {prefix} | head -n {limit} | while IFS= read -r c; do "
            'if [ -d "$c" ]; then printf "%s/\\n" "$c"; else printf "%s\\n" "$c"; fi; done'
        )
        fallback = (
            "for c in " + _glob(word.text) + "; do "
            '[ -e "$c" ] || continue; '
            'if [ -d "$c" ]; then printf "%s/\\n" "$c"; else printf "%s\\n" "$c"; fi; done '
            f"| head -n {limit}"
        )
    return (
        f"{go}if command -v bash >/dev/null 2>&1; then bash -c {shlex.quote(bash)}; "
        f"else {fallback}; fi"
    )


def _glob(text: str) -> str:
    """Шаблон "text*" для shell: text в кавычках (его спецсимволы буквально)."""
    if not text:
        return "*"
    directory, _, name = text.rpartition("/")
    quoted = shlex.quote(name) + "*" if name else "*"
    if directory or text.startswith("/"):
        return shlex.quote(directory + "/") + quoted
    return quoted


@dataclass(frozen=True)
class Completion:
    word: Word
    candidates: list[str]
    truncated: bool

    @property
    def common(self) -> str:
        """Общее начало всех кандидатов: его можно вставить сразу."""
        if not self.candidates:
            return self.word.text
        first, last = min(self.candidates), max(self.candidates)
        size = 0
        while size < min(len(first), len(last)) and first[size] == last[size]:
            size += 1
        return first[:size]

    def as_dict(self) -> dict[str, Any]:
        return {
            "word": self.word.text,
            "start": self.word.start,
            "kind": self.word.kind,
            "candidates": self.candidates,
            "insert": [quote_word(item) for item in self.candidates],
            "common": self.common,
            "truncated": self.truncated,
        }


def quote_word(text: str) -> str:
    """Как вставить кандидата в строку: спецсимволы shell экранируются."""
    return re.sub(r"([\s'\"\\$`!*?&;|<>()\[\]{}#~])", r"\\\1", text)


def parse_completion(word: Word, stdout: str) -> Completion:
    lines = [line for line in stdout.splitlines() if line]
    seen: dict[str, None] = {}
    for line in lines:
        seen.setdefault(line, None)
    candidates = sorted(seen, key=lambda c: (c.casefold(), c))
    truncated = len(candidates) > MAX_CANDIDATES
    return Completion(word=word, candidates=candidates[:MAX_CANDIDATES], truncated=truncated)


# ----------------------------------------------------------------- commands


async def run_user_command(
    manager: SSHSessionManager,
    session_id: str | None,
    request: SSHCommandRequest,
    *,
    source: str,
    request_id: str | None = None,
) -> ExecResult:
    """Команда пользователя из терминала или API: проверка на программы, которым нужен
    терминал, история сервера, выполнение."""
    session = manager.resolve(session_id)
    if request.track_cwd and not request.allow_interactive:
        found = interactive_program(request.command)
        if found is not None:
            error = SSHInteractiveCommandError(
                f"{found.program} needs a real terminal, and Synex runs commands without "
                f"one: it would hang until the timeout. {found.hint} To send it anyway, "
                "pass allow_interactive=true."
            )
            error.extra = {"program": found.program, "hint": found.hint}
            raise error
    get_history().add(history_key(session.username, session.host, session.port), request.command)
    timeout = request.timeout
    if timeout is None and source == "ws":
        timeout = settings.TERMINAL_TIMEOUT  # терминал: долгие команды, есть Ctrl+C
    return await manager.execute(
        session.id,
        request.command,
        timeout=timeout,
        track_cwd=request.track_cwd,
        source=source,
        request_id=request_id,
    )


COMPLETION_TIMEOUT = 5.0
COMPLETION_WAIT = 0.5  # занята командой: дополнять нечего ждать


async def complete(
    manager: SSHSessionManager, session_id: str, line: str, cursor: int | None
) -> Completion:
    session = manager.get(session_id)
    position = len(line) if cursor is None else max(0, min(cursor, len(line)))
    word = current_word(line, position)
    result = await manager.run_service(
        session.id,
        completion_command(word, session.cwd),
        timeout=COMPLETION_TIMEOUT,
        wait=COMPLETION_WAIT,
        name="terminal",
    )
    return parse_completion(word, result.stdout)
