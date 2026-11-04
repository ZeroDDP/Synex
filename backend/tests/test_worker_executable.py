"""Поиск бинарника воркера: формат файла, архитектура, сборка cargo --target, chmod +x."""

from __future__ import annotations

import os
import shlex
import stat
import struct
import sys
from pathlib import Path

import pytest

import app.worker.executable as executable
from app.core.config import settings
from app.worker.executable import (
    ELF,
    MACHO,
    PE,
    SCRIPT,
    BinaryFormat,
    ensure_executable,
    find_executable,
    format_problem,
    read_format,
    target_triples,
)
from app.worker.manager import WorkerManager
from tests.conftest import FAKE_WORKER_SCRIPT

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")


def elf(machine: int) -> bytes:
    header = bytearray(b"\x7fELF\x02\x01\x01" + b"\x00" * 57)
    struct.pack_into("<H", header, 18, machine)
    return bytes(header)


def macho(cpu: int) -> bytes:
    return b"\xcf\xfa\xed\xfe" + struct.pack("<I", cpu) + b"\x00" * 24


ELF_X86_64 = elf(0x3E)
ELF_AARCH64 = elf(0xB7)
MACHO_ARM64 = macho(0x0100000C)
MACHO_X86_64 = macho(0x01000007)
MACHO_UNIVERSAL = b"\xca\xfe\xba\xbe" + b"\x00" * 28
PE_EXE = b"MZ\x90\x00" + b"\x00" * 60
SHELL_SCRIPT = b"#!/bin/sh\nexec true\n"


def host(monkeypatch, os_name: str, arch: str) -> None:
    monkeypatch.setattr(executable, "host_os", lambda: os_name)
    monkeypatch.setattr(executable, "host_arch", lambda: arch)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (ELF_X86_64, BinaryFormat(ELF, "x86_64")),
        (ELF_AARCH64, BinaryFormat(ELF, "aarch64")),
        (MACHO_ARM64, BinaryFormat(MACHO, "aarch64")),
        (MACHO_X86_64, BinaryFormat(MACHO, "x86_64")),
        (MACHO_UNIVERSAL, BinaryFormat(MACHO)),
        (PE_EXE, BinaryFormat(PE)),
        (SHELL_SCRIPT, BinaryFormat(SCRIPT)),
        (b"hello", None),
    ],
)
def test_read_format(tmp_path: Path, content: bytes, expected):
    path = tmp_path / "file"
    path.write_bytes(content)
    assert read_format(path) == expected


@pytest.mark.parametrize(
    ("fmt", "os_name", "arch", "problem"),
    [
        (BinaryFormat(ELF, "x86_64"), "linux", "x86_64", None),
        (BinaryFormat(PE), "linux", "x86_64", "built for Windows"),
        (BinaryFormat(ELF, "aarch64"), "linux", "x86_64", "built for aarch64"),
        (BinaryFormat(SCRIPT), "linux", "x86_64", None),
        (BinaryFormat(MACHO, "aarch64"), "macos", "aarch64", None),
        (BinaryFormat(MACHO, "x86_64"), "macos", "aarch64", None),  # Rosetta 2
        (BinaryFormat(MACHO, "aarch64"), "macos", "x86_64", "built for aarch64"),
        (BinaryFormat(MACHO), "macos", "x86_64", None),  # universal
        (BinaryFormat(ELF, "x86_64"), "macos", "aarch64", "built for Linux"),
        (BinaryFormat(PE), "windows", "x86_64", None),
        (BinaryFormat(SCRIPT), "windows", "x86_64", "POSIX shell"),
        (None, "linux", "x86_64", "not an executable"),
    ],
)
def test_format_problem(monkeypatch, fmt, os_name, arch, problem):
    host(monkeypatch, os_name, arch)
    result = format_problem(fmt, os_name)
    assert (result is None) if problem is None else (problem in result)


def test_target_triples():
    assert target_triples("linux", "x86_64")[0] == "x86_64-unknown-linux-gnu"
    assert target_triples("macos", "aarch64")[0] == "aarch64-apple-darwin"
    assert target_triples("windows", "x86_64")[0] == "x86_64-pc-windows-msvc"


def test_wrong_format_is_skipped_and_cargo_target_build_is_found(monkeypatch, tmp_path: Path):
    host(monkeypatch, "linux", "x86_64")
    plain = tmp_path / "target" / "release" / "synex_worker"
    plain.parent.mkdir(parents=True)
    plain.write_bytes(PE_EXE)  # например, переименованный synex_worker.exe
    cross = tmp_path / "target" / "x86_64-unknown-linux-gnu" / "release" / "synex_worker"
    cross.parent.mkdir(parents=True)
    cross.write_bytes(ELF_X86_64)

    found = find_executable("./target/release/synex_worker", tmp_path)
    assert found.path == cross.resolve()
    assert "built for Windows" in found.skipped[str(tmp_path / "target/release/synex_worker")]


def test_nothing_suitable(monkeypatch, tmp_path: Path):
    host(monkeypatch, "macos", "aarch64")
    path = tmp_path / "release" / "synex_worker"
    path.parent.mkdir()
    path.write_bytes(ELF_X86_64)
    found = find_executable(str(path), tmp_path)
    assert found.path is None
    assert found.skipped == {str(path): "built for Linux, need a Mach-O binary for macos"}
    assert str(tmp_path / "aarch64-apple-darwin" / "release" / "synex_worker") in found.tried


def test_manager_does_not_start_wrong_binary(monkeypatch, tmp_path: Path):
    wrong = tmp_path / "synex_worker"
    wrong.write_bytes(b"not a binary")
    monkeypatch.setattr(settings, "RUST_EXECUTABLE_PATH_UNIX", str(wrong))
    monkeypatch.setattr(settings, "RUST_EXECUTABLE_PATH_WIN32", str(wrong))
    assert WorkerManager()._worker_command() is None


@posix_only
def test_missing_exec_bit_is_added(tmp_path: Path):
    path = tmp_path / "synex_worker"
    path.write_bytes(SHELL_SCRIPT)
    path.chmod(0o644)
    assert ensure_executable(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o755
    assert os.access(path, os.X_OK)


@posix_only
async def test_worker_without_exec_bit_starts(monkeypatch, worker_settings, tmp_path: Path):
    """Скрипт-обёртка без +x (как после распаковки zip): backend сам делает его
    исполняемым, запускает и подключается."""
    script = tmp_path / "release" / "synex_worker"
    script.parent.mkdir()
    command = f"exec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_WORKER_SCRIPT))}"
    script.write_text(f"#!/bin/sh\n{command}\n")
    script.chmod(0o644)
    monkeypatch.setattr(settings, "RUST_EXECUTABLE_PATH_UNIX", str(script))

    manager = WorkerManager()
    await manager.start()
    try:
        assert manager.status()["connected"] is True
        result = await manager.client.request("test_sleep", {"seconds": 0, "echo": "hi"})
        assert result["stdout"] == "hi"
    finally:
        await manager.stop()
