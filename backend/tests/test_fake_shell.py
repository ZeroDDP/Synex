"""FakeShell (эмулятор shell фейкового воркера) отвечает так же, как настоящий sh.

На Windows тесты команд идут через FakeShell. Здесь, на Linux и macOS, одни и те же
обёрнутые команды выполняются настоящим `sh -c` и FakeShell, результаты сравниваются.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.plugins.ssh.wrapper import build_command, new_marker, parse_output
from tests.fake_shell import HOME, FakeShell, parse_wrapped


def test_parse_wrapped():
    marker = new_marker()
    script = build_command("cd app && echo 'a b'", "/srv/my dir", marker, timeout=7)
    wrapped = parse_wrapped(script)
    assert wrapped is not None
    assert (wrapped.marker, wrapped.cwd, wrapped.command, wrapped.timeout) == (
        marker,
        "/srv/my dir",
        "cd app && echo 'a b'",
        7,
    )
    assert parse_wrapped("ls -la") is None


def test_raw_command_runs_in_home(tmp_path: Path):
    shell = FakeShell(tmp_path)
    assert shell.run("pwd; cd /tmp; pwd") == (f"{HOME}\n/tmp\n", "", 0)
    assert shell.run("false || echo fallback && echo next") == ("fallback\nnext\n", "", 0)
    assert shell.run("nosuch")[2] == 127


CASES = [
    ("pwd", None),
    ("cd /tmp && pwd", None),
    ("mkdir -p app && cd app && pwd", None),
    ("echo two >&2; exit 3", None),
    ("echo one; echo two", None),
    ("ls nope", None),
    ("nosuchcommand", None),
    ("false", None),
    ("false && echo no", None),
    ("false || echo yes", None),
    ("true", None),
    ("sh -c 'exit 42'", None),
    ("cd nope", None),
    ("pwd", "/definitely/missing"),
    ("cd ..", "/tmp"),
    ("echo started; sleep 3; echo never", "timeout"),
]
# Коды ошибок, которые у разных sh разные (ls и cd: 2 в dash/GNU, 1 в bash и на macOS).
# FakeShell отвечает как dash; для них сравнивается только «успех или ошибка».
LOOSE_CODES = {"ls nope", "cd nope"}


@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("sh"), reason="needs sh")
@pytest.mark.parametrize(("command", "cwd"), CASES)
def test_same_result_as_real_shell(tmp_path: Path, command: str, cwd: str | None):
    timeout = 10
    if cwd == "timeout":
        if not shutil.which("timeout"):
            pytest.skip("no coreutils timeout")
        cwd, timeout = None, 1

    home = tmp_path / "home"
    home.mkdir()
    real_home = str(home.resolve())
    marker = new_marker()

    def real_cwd(path: str | None) -> str | None:
        return path.replace(HOME, real_home, 1) if path and path.startswith(HOME) else path

    script = build_command(command, real_cwd(cwd), marker, timeout)
    env = {**os.environ, "HOME": real_home, "SHELL": "/bin/sh"}
    real = subprocess.run(
        ["sh", "-c", script], cwd=home, env=env, capture_output=True, text=True, timeout=30
    )
    fake_stdout, fake_stderr, fake_code = FakeShell(tmp_path / "fs").run(
        build_command(command, cwd, marker, timeout)
    )

    expected = parse_output(real.stdout, marker, timeout_applied=True)
    actual = parse_output(fake_stdout, marker, timeout_applied=True)
    assert actual.stdout == expected.stdout.replace(real_home, HOME)
    assert actual.timed_out == expected.timed_out
    assert actual.cwd == (expected.cwd.replace(real_home, HOME) if expected.cwd else None)
    if command in LOOSE_CODES:
        assert (actual.exit_code == 0) == (expected.exit_code == 0)
    else:
        assert actual.exit_code == expected.exit_code
        assert fake_code == real.returncode
    assert bool(fake_stderr) == bool(real.stderr)
