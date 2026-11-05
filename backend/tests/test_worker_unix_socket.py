"""Unix socket воркера на Linux и macOS: личная папка, мёртвый сокет, длина пути и
полный цикл с процессом воркера (запуск, падение, перезапуск, остановка)."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import stat
import sys
import tempfile
from pathlib import Path

import pytest

import app.worker.manager as manager_module
from app.core.config import default_socket_path, settings
from app.worker.manager import WorkerManager
from app.worker.unix_socket import SocketLocation, remove_stale_socket
from tests.conftest import FAKE_WORKER_SCRIPT
from tests.test_worker_manager import wait_until

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="Unix sockets")


def test_default_socket_path():
    linux = {"XDG_RUNTIME_DIR": "/run/user/1000"}
    assert default_socket_path("linux", linux, 1000) == Path("/run/user/1000/synex/worker.sock")
    assert default_socket_path("linux", {}, 1000) == Path("/tmp/synex-1000/worker.sock")
    assert default_socket_path("linux", {"XDG_RUNTIME_DIR": "rel"}, 7) == Path(
        "/tmp/synex-7/worker.sock"
    )
    macos = {"TMPDIR": "/var/folders/ab/xyz/T/", "XDG_RUNTIME_DIR": "/ignored"}
    expected = Path("/var/folders/ab/xyz/T/synex/worker.sock")
    assert default_socket_path("darwin", macos, 501) == expected


@pytest.fixture
def short_dir():
    """Короткий путь: на macOS tmp_path pytest длиннее лимита в 104 байта для сокета."""
    path = Path(tempfile.mkdtemp(prefix="sx", dir="/tmp" if os.path.isdir("/tmp") else None))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def dead_socket(path: Path) -> None:
    """Файл сокета, который никто не слушает (как после падения воркера)."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.bind(str(path))


@posix_only
def test_private_dir_is_created(short_dir: Path):
    location = SocketLocation("", short_dir / "synex" / "worker.sock")
    assert location.prepare() == short_dir / "synex" / "worker.sock"
    assert mode(short_dir / "synex") == 0o700


@posix_only
def test_loose_permissions_are_fixed(short_dir: Path):
    (short_dir / "synex").mkdir(mode=0o755)
    (short_dir / "synex").chmod(0o755)
    SocketLocation("", short_dir / "synex" / "worker.sock").prepare()
    assert mode(short_dir / "synex") == 0o700


@posix_only
@pytest.mark.parametrize("problem", ["symlink", "foreign_owner", "too_long", "is_a_file"])
def test_unsafe_default_dir_is_replaced_by_temp_dir(short_dir: Path, monkeypatch, problem):
    default = short_dir / "synex" / "worker.sock"
    if problem == "symlink":
        (short_dir / "elsewhere").mkdir()
        (short_dir / "synex").symlink_to(short_dir / "elsewhere")
    elif problem == "foreign_owner":
        monkeypatch.setattr(os, "getuid", lambda: os.geteuid() + 1)
    elif problem == "too_long":
        default = short_dir / ("x" * 120) / "worker.sock"
    else:
        (short_dir / "synex").write_text("")

    location = SocketLocation("", default)
    path = location.prepare()
    assert path.parent != default.parent
    assert path.name == "worker.sock"
    assert mode(path.parent) == 0o700
    assert len(os.fsencode(path)) <= 103

    location.cleanup()
    assert not path.parent.exists()
    assert location.path == default


@posix_only
def test_stale_socket_is_removed_live_one_is_kept(short_dir: Path):
    stale = short_dir / "stale.sock"
    dead_socket(stale)
    remove_stale_socket(stale)
    assert not stale.exists()

    live = short_dir / "live.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(live))
        server.listen()
        remove_stale_socket(live)
        assert live.exists()

    regular = short_dir / "regular"
    regular.write_text("data")
    remove_stale_socket(regular)
    assert regular.read_text() == "data"


@posix_only
def test_explicit_path_dir_is_created_but_not_chmodded(short_dir: Path):
    shared = short_dir / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    location = SocketLocation(str(shared / "w.sock"), short_dir / "unused" / "worker.sock")
    assert location.prepare() == shared / "w.sock"
    assert mode(shared) == 0o777  # явно заданную папку не трогаем

    nested = SocketLocation(str(short_dir / "new" / "w.sock"), short_dir / "unused" / "x")
    nested.prepare()
    assert (short_dir / "new").is_dir()


@posix_only
async def test_worker_over_unix_socket(monkeypatch, worker_settings, short_dir: Path):
    """Полный цикл: воркер слушает сокет в личной папке, после падения оставляет файл
    сокета, перезапущенный воркер снова слушает тот же путь, а не уходит на TCP."""
    socket_path = short_dir / "synex" / "worker.sock"
    monkeypatch.setattr(settings, "RUST_IPC_SOCKET_PATH", "")
    monkeypatch.setattr(manager_module, "default_socket_path", lambda: socket_path)
    monkeypatch.setenv("FAKE_WORKER_TRANSPORT", "unix")
    monkeypatch.setattr(manager_module, "SUPERVISE_INTERVAL", 0.2)

    manager = WorkerManager()
    monkeypatch.setattr(
        manager, "_worker_command", lambda: [sys.executable, str(FAKE_WORKER_SCRIPT)]
    )
    await manager.start()
    try:
        assert manager.client.transport == "unix"
        assert manager.socket_path == str(socket_path)
        assert mode(socket_path.parent) == 0o700
        channel = await manager.open_channel()
        assert channel.transport == "unix"
        await channel.disconnect()

        first_pid = manager.status()["worker_pid"]
        os.kill(first_pid, signal.SIGKILL)  # файл сокета остаётся на месте
        await wait_until(
            lambda: (
                manager.status()["worker_pid"] not in (None, first_pid)
                and manager.status()["connected"]
            ),
            timeout=15,
        )
        assert manager.client.transport == "unix"
        result = await manager.client.request("test_sleep", {"seconds": 0, "echo": "back"})
        assert result["stdout"] == "back"
    finally:
        await manager.stop()
    assert not socket_path.exists()
