from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.plugins.ssh.wrapper import build_command, new_marker, normalize_newlines, parse_output

M = "__SYNEX_test__:"


def test_without_cwd_there_is_no_cd():
    script = build_command("ls -la", None, M, timeout=5)
    assert "cd --" not in script
    assert "timeout -k 3 5" in script


def test_parse_variants():
    def parse(text: str, applied: bool = True):
        result = parse_output(text, M, timeout_applied=applied)
        return result.stdout, result.exit_code, result.cwd, result.timed_out

    # обычное завершение: оба маркера
    assert parse(f"hello\n\n{M}C0|/root\n{M}E0") == ("hello", 0, "/root", False)
    assert parse(f"\n{M}C2|/a|b\n{M}E2") == ("", 2, "/a|b", False)
    # `exit 3` внутри команды: только внешний маркер, cwd не меняется
    assert parse(f"out\n{M}E3") == ("out", 3, None, False)
    # прервано timeout: TERM (124) или KILL (137)
    assert parse(f"partial\n{M}E124") == ("partial", 124, None, True)
    assert parse(f"partial\n{M}E137") == ("partial", 137, None, True)
    assert parse(f"partial\n{M}E124", applied=False)[3] is False
    # команда сама вернула 124 и дошла до конца: это не таймаут
    assert parse(f"\n{M}C124|/x\n{M}E124") == ("", 124, "/x", False)
    # маркеров нет
    assert parse("  indented\nline") == ("  indented\nline", None, None, False)


def test_normalize_newlines():
    assert normalize_newlines("cd /tmp\r\nls\r\n") == "cd /tmp\nls\n"
    assert normalize_newlines("echo a\recho b") == "echo a\necho b"
    assert normalize_newlines("printf 'a\\rb'") == "printf 'a\\rb'"  # escape, не символ CR


def test_markers_are_unique():
    assert new_marker() != new_marker()


SHELLS = [s for s in ("sh", "bash", "dash", "zsh") if shutil.which(s)]


class Session:
    """Мини-эмуляция /api/ssh/exec поверх локального shell."""

    def __init__(self, shell: str, home: Path, path_prefix: str | None = None) -> None:
        self.shell = shutil.which(shell)
        self.home = home
        self.cwd: str | None = None
        self.marker = new_marker()
        path = os.environ["PATH"] if path_prefix is None else f"{path_prefix}:{os.environ['PATH']}"
        self.env = {**os.environ, "HOME": str(home), "SHELL": self.shell, "PATH": path}

    def run(self, command: str, timeout: int | None = 5, kill_after: int = 1):
        script = build_command(command, self.cwd, self.marker, timeout, kill_after=kill_after)
        started = time.monotonic()
        proc = subprocess.run(
            [self.shell, "-c", script],
            cwd=self.home,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        elapsed = time.monotonic() - started
        result = parse_output(proc.stdout, self.marker, timeout_applied=timeout is not None)
        if result.cwd:
            self.cwd = result.cwd
        # код из маркеров совпадает с настоящим кодом процесса
        assert result.exit_code == proc.returncode
        return result, proc.stderr.rstrip("\n"), elapsed


posix_only = pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX shell")


@posix_only
@pytest.mark.parametrize("shell", SHELLS)
def test_cwd_with_special_characters(shell: str, tmp_path: Path):
    tricky = tmp_path / "a 'b' \"c\" $HOME `id` ; rm -rf x"
    tricky.mkdir()
    s = Session(shell, tmp_path)
    s.cwd = str(tricky)
    result, stderr, _ = s.run("pwd")
    assert (result.stdout, stderr, result.exit_code) == (str(tricky), "", 0)


@posix_only
@pytest.mark.parametrize("shell", SHELLS)
def test_cwd_and_exit_codes(shell: str, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    real_home = str(home.resolve())
    s = Session(shell, home)

    result, stderr, _ = s.run("pwd")
    assert (result.stdout, result.exit_code, stderr) == (real_home, 0, "")
    assert s.cwd == real_home

    s.run('mkdir -p "it\'s dir" && cd "it\'s dir"')
    assert s.cwd == f"{real_home}/it's dir"
    assert s.run("pwd")[0].stdout == f"{real_home}/it's dir"

    assert s.run("false")[0].exit_code == 1
    assert s.run("ls /definitely/missing")[0].exit_code != 0
    assert s.cwd == f"{real_home}/it's dir"

    # exit внутри команды: код настоящий, cwd прежняя
    result, _, _ = s.run("cd / && exit 3")
    assert (result.exit_code, result.cwd, result.timed_out) == (3, None, False)
    assert s.cwd == f"{real_home}/it's dir"

    result, stderr, _ = s.run("echo out; echo err >&2; cd ..")
    assert (result.stdout, stderr, result.exit_code) == ("out", "err", 0)
    assert s.cwd == real_home

    assert s.run("cd /tmp\npwd # comment")[0].stdout == "/tmp"

    # Директория исчезла: команда НЕ выполняется, уходим в home
    gone = home / "gone"
    gone.mkdir()
    s.run(f"cd '{gone}'")
    gone.rmdir()
    result, stderr, _ = s.run("touch must_not_exist")
    assert result.exit_code == 1 and "not available" in stderr
    assert s.cwd == real_home
    assert not (home / "must_not_exist").exists()


@posix_only
@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("timeout", [5, None])
def test_command_does_not_wait_for_input(shell: str, timeout: int | None, tmp_path: Path):
    """Воркер не закрывает stdin канала SSH. Без </dev/null `cat` или `read` (а с ними
    sudo с паролем, apt install с вопросом) ждали бы ввода до таймаута."""
    s = Session(shell, tmp_path)
    script = build_command("cat; read x || echo no-input", None, s.marker, timeout)
    started = time.monotonic()
    proc = subprocess.Popen(
        [s.shell, "-c", script],
        cwd=tmp_path,
        env=s.env,
        stdin=subprocess.PIPE,  # открыт и пуст, как у канала SSH
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stdin.close()
    stdout = proc.stdout.read()
    proc.stdout.close()
    proc.stderr.close()
    result = parse_output(stdout, s.marker, timeout_applied=timeout is not None)
    assert (result.stdout, result.exit_code, result.timed_out) == ("no-input", 0, False)
    assert time.monotonic() - started < 3


@posix_only
@pytest.mark.skipif(shutil.which("timeout") is None, reason="no coreutils timeout (macOS)")
@pytest.mark.parametrize("shell", SHELLS)
def test_timeout_stops_command_on_server(shell: str, tmp_path: Path):
    s = Session(shell, tmp_path)

    result, stderr, elapsed = s.run("echo started; sleep 30", timeout=1)
    assert result.timed_out and result.exit_code == 124
    assert result.stdout == "started"  # вывод до прерывания сохраняется
    assert stderr == ""  # без "Killed" и текста обёртки
    assert elapsed < 5

    # Дочерние процессы тоже прерываются, иначе канал SSH не закрылся бы
    result, _, elapsed = s.run("sh -c 'sleep 30; echo late' & wait", timeout=1)
    assert result.timed_out and elapsed < 5 and "late" not in result.stdout

    # Команда игнорирует TERM: через kill_after приходит KILL
    result, stderr, elapsed = s.run("trap '' TERM; sleep 30", timeout=1, kill_after=1)
    assert result.timed_out and result.exit_code == 137
    assert stderr == ""
    assert elapsed < 6

    # Команда умерла от TERM, а её дочерний процесс TERM игнорирует и держит вывод:
    # обёртка добивает группу (GNU timeout здесь KILL не шлёт, uutils шлёт только ребёнку)
    result, _, elapsed = s.run("sh -c \"trap '' TERM; sleep 30\" & wait", timeout=1)
    assert result.timed_out and elapsed < 5

    # Быстрая команда в пределах лимита
    result, _, _ = s.run("echo fast", timeout=10)
    assert (result.stdout, result.timed_out) == ("fast", False)


@posix_only
def test_without_timeout_utility_command_still_runs(tmp_path: Path):
    """На сервере нет рабочего `timeout`: команда выполняется без лимита на сервере."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    broken = fake_bin / "timeout"
    broken.write_text("#!/bin/sh\nexit 127\n")
    broken.chmod(0o755)
    s = Session("sh", tmp_path, path_prefix=str(fake_bin))

    result, _, _ = s.run("echo works; exit 0", timeout=1)
    assert (result.stdout, result.exit_code, result.timed_out) == ("works", 0, False)
    result, _, _ = s.run("cd /tmp", timeout=1)
    assert s.cwd == "/tmp"
