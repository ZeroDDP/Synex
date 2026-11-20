"""Терминал UI поверх команд сессии: защита от полноэкранных программ и свой лимит
времени для команд терминала.

Команда терминала выполняется без TTY и без ввода (wrapper.py), поэтому полноэкранные
программы (vim, top, tmux) работать не могут: висят до таймаута или сразу падают, а их
escape-коды ломают экран xterm в UI. Такие команды backend не отправляет и подсказывает
замену (422 ssh_interactive_command); allow_interactive=true отправляет всё равно.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from app.core.config import settings
from app.plugins.ssh.errors import SSHInteractiveCommandError
from app.plugins.ssh.manager import ExecResult, SSHSessionManager
from app.plugins.ssh.schemas import SSHCommandRequest

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
    терминал, выполнение (у команды терминала свой лимит времени)."""
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
