"""Поиск бинарника synex_worker для текущей ОС и процессора.

Где ищем:
1. RUST_EXECUTABLE_PATH_WIN32 / RUST_EXECUTABLE_PATH_UNIX: относительно backend/, затем
   относительно текущей папки.
2. Сборка cargo с --target: если путь заканчивается на .../release/<имя> (или debug),
   то .../<target triple>/release/<имя>. Например, на Mac с Apple Silicon
   app/core/target/aarch64-apple-darwin/release/synex_worker.
3. Сборка из исходников воркера в этом же репозитории: core/target/release/<имя> и
   core/target/<target triple>/release/<имя> (после `cargo build --release` в core/).

Из всех подходящих файлов берётся самый свежий (по времени изменения): только что
собранный в core/ воркер новее бинарника из репозитория, а после git pull новее
становится уже он. Если путь в настройках задан не по умолчанию, используется только он.

Файл проверяется по первым байтам. В репозитории лежит synex_worker.exe для Windows:
на Linux и macOS его запуск закончился бы непонятным "Exec format error", поэтому
неподходящий файл пропускается, а в логе написано, что с ним не так. Shell-скрипт
(#!) на Linux и macOS подходит: так удобно запускать воркер через обёртку.

Если у файла нет права на запуск (распакован из zip, пришёл из git на Windows),
backend ставит его сам (chmod +x).
"""

from __future__ import annotations

import os
import platform
import stat
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

import structlog

logger = structlog.get_logger()

# Форматы исполняемых файлов
ELF = "ELF"  # Linux
MACHO = "Mach-O"  # macOS
PE = "PE"  # Windows
SCRIPT = "script"  # #!/bin/sh и т.п.

_ELF_MACHINES = {0x3E: "x86_64", 0xB7: "aarch64", 0x03: "x86", 0x28: "arm"}
_MACHO_CPUS = {0x01000007: "x86_64", 0x0100000C: "aarch64", 7: "x86", 12: "arm"}


@dataclass(frozen=True)
class BinaryFormat:
    kind: str  # ELF | Mach-O | PE | script
    arch: str | None = None  # x86_64 | aarch64 | ...; None: неизвестно или universal


def host_os() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def host_arch() -> str:
    machine = platform.machine().lower()
    return {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(machine, machine)


def target_triples(os_name: str | None = None, arch: str | None = None) -> list[str]:
    """Target triple Rust для этой машины (как в `cargo build --target ...`)."""
    os_name = os_name or host_os()
    arch = arch or host_arch()
    if os_name == "windows":
        return [f"{arch}-pc-windows-msvc", f"{arch}-pc-windows-gnu"]
    if os_name == "macos":
        return [f"{arch}-apple-darwin", "universal-apple-darwin"]
    return [f"{arch}-unknown-linux-gnu", f"{arch}-unknown-linux-musl"]


def expected_kind(os_name: str | None = None) -> str:
    return {"windows": PE, "macos": MACHO}.get(os_name or host_os(), ELF)


def read_format(path: Path) -> BinaryFormat | None:
    """Формат файла по первым байтам (None: не исполняемый файл)."""
    try:
        with path.open("rb") as f:
            head = f.read(64)
    except OSError:
        return None

    if head.startswith(b"#!"):
        return BinaryFormat(SCRIPT)
    if head.startswith(b"\x7fELF") and len(head) >= 20:
        order = "<" if head[5] == 1 else ">"
        (machine,) = struct.unpack_from(f"{order}H", head, 18)
        return BinaryFormat(ELF, _ELF_MACHINES.get(machine))
    if head[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe") and len(head) >= 8:
        (cpu,) = struct.unpack_from("<I", head, 4)
        return BinaryFormat(MACHO, _MACHO_CPUS.get(cpu))
    if head[:4] == b"\xca\xfe\xba\xbe":
        return BinaryFormat(MACHO)  # universal binary: несколько архитектур сразу
    if head.startswith(b"MZ"):
        return BinaryFormat(PE)
    return None


def format_problem(fmt: BinaryFormat | None, os_name: str | None = None) -> str | None:
    """Почему файл нельзя запустить здесь (None: можно)."""
    os_name = os_name or host_os()
    if fmt is None:
        return "not an executable file"
    if fmt.kind == SCRIPT and os_name != "windows":
        return None
    want = expected_kind(os_name)
    if fmt.kind != want:
        built_for = {PE: "Windows", MACHO: "macOS", ELF: "Linux", SCRIPT: "a POSIX shell"}
        return f"built for {built_for[fmt.kind]}, need a {want} binary for {os_name}"
    arch = host_arch()
    if fmt.arch is None or fmt.arch == arch:
        return None
    if os_name == "macos" and fmt.arch == "x86_64" and arch == "aarch64":
        return None  # Rosetta 2 запустит x86_64-сборку на Apple Silicon
    return f"built for {fmt.arch}, this machine is {arch}"


@dataclass
class Discovery:
    path: Path | None = None
    tried: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)  # путь → причина


def candidates(configured: str, base_dir: Path, *, include_core: bool = True) -> list[Path]:
    """Где искать воркер. base_dir это папка backend/; core/ лежит рядом с ней."""
    raw = Path(configured)
    paths: list[Path] = []
    for base in [None] if raw.is_absolute() else [base_dir, Path.cwd()]:
        path = raw if base is None else base / raw
        paths.append(path)
        if path.parent.name in ("release", "debug"):
            target_dir = path.parent.parent
            for triple in target_triples():
                paths.append(target_dir / triple / path.parent.name / path.name)
    if include_core:
        names = {
            ("windows", "x86_64"): "synex_worker_windows.exe",
            ("linux", "x86_64"): "synex_worker_linux",
            ("macos", "x86_64"): "synex_worker_x84_64-apple-darwin",
            ("macos", "aarch64"): "synex_worker_aarch64-apple-darwin",
        }
        bundled = base_dir / "app/core/target/release"
        name = names.get((host_os(), host_arch()))
        if name:
            paths.append(bundled / name)
        if host_os() == "macos":
            paths.append(bundled / "synex_worker_macOs")
        core_target = base_dir.parent / "core" / "target"
        paths.append(core_target / "release" / raw.name)
        paths.extend(core_target / triple / "release" / raw.name for triple in target_triples())
    return list(dict.fromkeys(paths))


def find_executable(configured: str, base_dir: Path, *, include_core: bool = True) -> Discovery:
    """Самый свежий подходящий воркер (см. docstring модуля)."""
    result = Discovery()
    found: list[tuple[float, Path]] = []
    for path in candidates(configured, base_dir, include_core=include_core):
        result.tried.append(str(path))
        if not path.is_file():
            continue
        problem = format_problem(read_format(path))
        if problem is not None:
            result.skipped[str(path)] = problem
            continue
        found.append((path.stat().st_mtime, path.resolve()))
    if found:
        result.path = max(found, key=lambda item: item[0])[1]
    return result


def ensure_executable(path: Path) -> bool:
    """Право на запуск на Linux/macOS: ставит +x, если его нет. False: поставить не удалось."""
    if sys.platform == "win32" or os.access(path, os.X_OK):
        return True
    try:
        mode = path.stat().st_mode
        # +x там же, где есть +r: rw-r--r-- → rwxr-xr-x
        path.chmod(mode | ((mode & 0o444) >> 2) | stat.S_IXUSR)
    except OSError as e:
        logger.error(
            "Rust worker is not executable and chmod failed. Run: chmod +x <path>",
            path=str(path),
            error=repr(e),
        )
        return False
    logger.info("Made Rust worker executable (chmod +x)", path=str(path))
    return True


def startup_failure_hint(returncode: int | None, output: list[str] | None = None) -> str | None:
    """Подсказка, почему воркер умер сразу после запуска (по коду выхода и его выводу)."""
    text = "\n".join(output or [])
    if "GLIBC_" in text and "not found" in text:
        return (
            "The worker was built against a newer glibc than this system has (Linux "
            "synex_worker from the repository needs glibc 2.38+: Ubuntu 24.04, Debian 13, "
            "Fedora 39 or newer). Build it here with `cargo build --release`, or build a "
            "static one: `cargo build --release --target x86_64-unknown-linux-musl`"
        )
    if sys.platform == "darwin" and returncode == -9:
        return (
            "macOS may have blocked an unsigned binary downloaded from the internet. "
            "Remove the quarantine flag: xattr -d com.apple.quarantine <path to synex_worker>"
        )
    return None
