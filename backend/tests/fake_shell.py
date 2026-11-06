"""Эмулятор POSIX shell для фейкового воркера: тесты команд идут и на Windows.

Фейковый воркер на Linux и macOS выполняет команды настоящим `sh -c`. На Windows sh
нет, поэтому там (или с SYNEX_TEST_SHELL=emulated) команды выполняет FakeShell:

* понимает обёртку из app/plugins/ssh/wrapper.py (cwd, маркеры C и E, timeout) и
  отвечает так же, как настоящий shell на сервере;
* сам набор команд маленький, ровно для тестов: cd, pwd, echo, ls, mkdir, cat, sleep,
  true, false, exit, hostname, sh -c; связки `;`, `&&`, `||`, перенаправление `>&2`;
* файловая система виртуальная: "/" это папка root, в ней есть /tmp и домашняя
  /home/synex. Пути всегда в стиле POSIX, на любой ОС.

Что совпадает с настоящим sh (dash), проверяет tests/test_fake_shell.py на Linux.
"""

from __future__ import annotations

import posixpath
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path

from app.plugins.ssh.wrapper import RC_VAR

HOME = "/home/synex"
_MARKER_RE = re.compile(r"__SYNEX_[0-9a-f]+__:")
_TIMEOUT_RE = re.compile(r'timeout -k \d+ (\d+) "\$\{SHELL')
_SEPARATORS = (";", "&&", "||")


class _TimedOutError(Exception):
    pass


@dataclass
class _Wrapped:
    marker: str
    cwd: str | None
    command: str
    timeout: int | None


@dataclass
class _Run:
    stdout: str = ""
    stderr: str = ""
    code: int = 0
    exited: bool = False  # команда вызвала exit: маркер C не печатается


def parse_wrapped(script: str) -> _Wrapped | None:
    """Разбирает команду, обёрнутую build_command (None: команда без обёртки)."""
    marker = _MARKER_RE.search(script)
    if marker is None or RC_VAR not in script:
        return None
    tokens = shlex.split(script)
    # Тело для "$SHELL -c" без таймаута: последний -c (ветка else или единственный)
    index = max(i for i, token in enumerate(tokens) if token == "-c")
    lines = tokens[index + 1].split("\n")
    if lines and lines[0] == "exec </dev/null":
        lines = lines[1:]  # stdin команды: в эмуляторе ввода и так нет
    cwd = None
    if lines and lines[0].startswith("cd -- "):
        cwd = shlex.split(lines[0])[2]
        lines = lines[1:]
    rc_line = max(i for i, line in enumerate(lines) if line.startswith(f"{RC_VAR}=$?"))
    timeout = _TIMEOUT_RE.search(script)
    return _Wrapped(
        marker=marker.group(0),
        cwd=cwd,
        command="\n".join(lines[:rc_line]),
        timeout=int(timeout.group(1)) if timeout else None,
    )


class FakeShell:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = HOME
        for directory in ("tmp", HOME.lstrip("/")):
            (root / directory).mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- entry

    def run(self, script: str) -> tuple[str, str, int]:
        """Как `sh -c script` на сервере: stdout, stderr, код возврата."""
        wrapped = parse_wrapped(script)
        if wrapped is None:
            result, _ = self._execute(script, self.home, deadline=None)
            return result.stdout, result.stderr, result.code
        return self._run_wrapped(wrapped)

    def _run_wrapped(self, w: _Wrapped) -> tuple[str, str, int]:
        if w.cwd is not None and not self._is_dir(w.cwd):
            stderr = f"synex: directory {w.cwd} is not available, switched to home\n"
            return f"\n{w.marker}C1|{self.home}\n{w.marker}E1", stderr, 1

        deadline = time.monotonic() + w.timeout if w.timeout else None
        cwd = w.cwd or self.home
        try:
            result, cwd = self._execute(w.command, cwd, deadline)
        except _TimedOutError as e:
            partial: _Run = e.args[0]
            return f"{partial.stdout}\n{w.marker}E124", partial.stderr, 124
        stdout = result.stdout
        if not result.exited:
            stdout += f"\n{w.marker}C{result.code}|{cwd}"
        return f"{stdout}\n{w.marker}E{result.code}", result.stderr, result.code

    # ----------------------------------------------------------- execution

    def _execute(self, command: str, cwd: str, deadline: float | None) -> tuple[_Run, str]:
        run = _Run()
        for op, words in self._parse(command):
            if (op == "&&" and run.code != 0) or (op == "||" and run.code == 0):
                continue
            to_stderr = words[-2:] == [">&", "2"]  # echo text >&2
            if to_stderr:
                words = words[:-2]
            try:
                out, err, code, cwd, exited = self._command(words, cwd, deadline)
            except _TimedOutError:
                raise _TimedOutError(run) from None
            if to_stderr:
                out, err = "", err + out
            run.stdout += out
            run.stderr += err
            run.code = code
            if exited:
                run.exited = True
                break
        return run, cwd

    @staticmethod
    def _parse(command: str) -> list[tuple[str, list[str]]]:
        """[(связка с предыдущей командой, слова)]."""
        lexer = shlex.shlex(command.replace("\n", ";"), posix=True, punctuation_chars=";&|>")
        lexer.whitespace_split = True
        items: list[tuple[str, list[str]]] = []
        op, words = ";", []
        for token in lexer:
            if token in _SEPARATORS:
                items.append((op, words))
                op, words = token, []
            else:
                words.append(token)
        items.append((op, words))
        return [(op, words) for op, words in items if words]

    def _command(
        self, words: list[str], cwd: str, deadline: float | None
    ) -> tuple[str, str, int, str, bool]:
        """stdout, stderr, код, новая cwd, был ли exit."""
        name, args = words[0], words[1:]
        if name == "cd":
            target = self._abs(cwd, args[0]) if args else self.home
            if not self._is_dir(target):
                return "", f"sh: 1: cd: can't cd to {args[0]}\n", 2, cwd, False
            return "", "", 0, target, False
        if name == "pwd":
            return f"{cwd}\n", "", 0, cwd, False
        if name == "echo":
            newline = not (args and args[0] == "-n")
            text = " ".join(args[0 if newline else 1 :])
            return text + ("\n" if newline else ""), "", 0, cwd, False
        if name == "exit":
            return "", "", int(args[0]) if args else 0, cwd, True
        if name in ("true", "false"):
            return "", "", int(name == "false"), cwd, False
        if name == "hostname":
            return "fake-host\n", "", 0, cwd, False
        if name == "sleep":
            self._sleep(float(args[0]), deadline)
            return "", "", 0, cwd, False
        if name == "mkdir":
            return (*self._mkdir(cwd, args), cwd, False)
        if name == "ls":
            return (*self._ls(cwd, args), cwd, False)
        if name == "cat":
            return (*self._cat(cwd, args), cwd, False)
        if name == "sh" and args[:1] == ["-c"]:
            result, _ = self._execute(args[1], cwd, deadline)  # подоболочка: cwd не меняется
            return result.stdout, result.stderr, result.code, cwd, False
        return "", f"sh: 1: {name}: not found\n", 127, cwd, False

    # ------------------------------------------------------------ builtins

    @staticmethod
    def _sleep(seconds: float, deadline: float | None) -> None:
        if deadline is not None and time.monotonic() + seconds > deadline:
            time.sleep(max(0.0, deadline - time.monotonic()))
            raise _TimedOutError
        time.sleep(seconds)

    def _mkdir(self, cwd: str, args: list[str]) -> tuple[str, str, int]:
        parents = "-p" in args
        for name in (a for a in args if a != "-p"):
            path = self._real(self._abs(cwd, name))
            if path.exists() and not parents:
                return "", f"mkdir: cannot create directory '{name}': File exists\n", 1
            path.mkdir(parents=parents, exist_ok=parents)
        return "", "", 0

    def _ls(self, cwd: str, args: list[str]) -> tuple[str, str, int]:
        out, err, code = "", "", 0
        for name in args or ["."]:
            path = self._real(self._abs(cwd, name))
            if not path.exists():
                err += f"ls: cannot access '{name}': No such file or directory\n"
                code = 2
            elif path.is_dir():
                out += "".join(f"{p.name}\n" for p in sorted(path.iterdir()))
            else:
                out += f"{name}\n"
        return out, err, code

    def _cat(self, cwd: str, args: list[str]) -> tuple[str, str, int]:
        out, err, code = "", "", 0
        for name in args:
            path = self._real(self._abs(cwd, name))
            if path.is_file():
                out += path.read_text()
            else:
                err += f"cat: {name}: No such file or directory\n"
                code = 1
        return out, err, code

    # ------------------------------------------------------------ paths

    @staticmethod
    def _abs(cwd: str, path: str) -> str:
        return posixpath.normpath(posixpath.join(cwd, path)).replace("//", "/")

    def _real(self, path: str) -> Path:
        return self.root.joinpath(*[p for p in path.split("/") if p])

    def _is_dir(self, path: str) -> bool:
        return self._real(path).is_dir()
