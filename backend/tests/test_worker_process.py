"""Процесс воркера на Linux и macOS: своя сессия (Ctrl+C), смерть вместе с backend
(Linux, PDEATHSIG), остановка воркера, оставшегося от прошлого запуска (PID-файл)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from app.core.config import settings
from app.worker.manager import WorkerManager
from app.worker.process import WorkerPidFile, is_alive
from tests.conftest import FAKE_WORKER_SCRIPT

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX processes")

WORKER_HOST = Path(__file__).with_name("worker_host.py")


@pytest.fixture
def socket_path(monkeypatch, worker_settings):
    """Сокет по короткому пути (на macOS tmp_path длиннее лимита) и воркер на нём."""
    directory = Path(tempfile.mkdtemp(prefix="sx", dir="/tmp"))
    path = directory / "worker.sock"
    monkeypatch.setattr(settings, "RUST_IPC_SOCKET_PATH", str(path))
    monkeypatch.setenv("FAKE_WORKER_TRANSPORT", "unix")
    yield path
    shutil.rmtree(directory, ignore_errors=True)


def fake_manager(monkeypatch) -> WorkerManager:
    manager = WorkerManager()
    monkeypatch.setattr(
        manager, "_worker_command", lambda: [sys.executable, str(FAKE_WORKER_SCRIPT)]
    )
    return manager


def wait_dead(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(0.05)
    return False


def start_host(socket_path: Path, *args: str) -> tuple[subprocess.Popen, int]:
    """Запускает «backend» в отдельном процессе и возвращает PID его воркера."""
    env = {
        **os.environ,
        "RUST_IPC_SOCKET_PATH": str(socket_path),
        "RUST_IPC_TCP_FALLBACK_PORT": str(settings.RUST_IPC_TCP_FALLBACK_PORT),
        "FAKE_WORKER_TRANSPORT": "unix",
    }
    host = subprocess.Popen(
        [sys.executable, str(WORKER_HOST), *args],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert host.stdout is not None
    deadline = time.monotonic() + 20
    for line in host.stdout:
        if line.startswith("WORKER_PID "):
            return host, int(line.split()[1])
        if time.monotonic() > deadline:
            break
    host.kill()
    raise AssertionError("worker host did not start")


def kill_host(host: subprocess.Popen) -> None:
    host.kill()
    host.wait(5)
    if host.stdout is not None:
        host.stdout.close()


async def test_worker_runs_in_its_own_session(monkeypatch, socket_path: Path):
    """Ctrl+C в терминале не долетает до воркера: у него своя сессия и группа."""
    manager = fake_manager(monkeypatch)
    await manager.start()
    try:
        pid = manager.status()["worker_pid"]
        assert os.getsid(pid) == pid
        assert os.getpgid(pid) != os.getpgid(0)
        pid_file = json.loads(Path(f"{socket_path}.pid").read_text())
        assert pid_file["worker_pid"] == pid
        assert pid_file["backend_pid"] == os.getpid()
        assert manager.client.transport == "unix"
    finally:
        await manager.stop()
    assert not Path(f"{socket_path}.pid").exists()
    assert not socket_path.exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG")
def test_worker_dies_with_killed_backend(socket_path: Path):
    host, worker_pid = start_host(socket_path)
    assert is_alive(worker_pid)
    kill_host(host)  # kill -9: backend не успевает ничего сделать
    assert wait_dead(worker_pid), "worker outlived the backend"


async def test_worker_left_from_previous_run_is_stopped(monkeypatch, socket_path: Path):
    """Как на macOS: backend убит, воркер остался и держит сокет. Следующий запуск
    находит его по PID-файлу, останавливает и поднимает свой воркер на том же сокете."""
    host, orphan = start_host(socket_path, "--no-pdeathsig")
    kill_host(host)
    time.sleep(0.3)
    assert is_alive(orphan)

    manager = fake_manager(monkeypatch)
    await manager.start()
    try:
        assert wait_dead(orphan, 1)
        assert manager.status()["worker_pid"] != orphan
        assert manager.client.transport == "unix"  # сокет освобождён, не ушли на TCP
    finally:
        await manager.stop()
        if is_alive(orphan):
            os.kill(orphan, 9)


def spawn_detached_sleep() -> int:
    """`sleep`, который не наш дочерний процесс (его подберёт init)."""
    output = subprocess.run(
        ["sh", "-c", "sleep 30 >/dev/null 2>&1 & echo $!"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(output.stdout)


def finished_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def write_pid_file(path: Path, worker_pid: int, backend_pid: int, executable: str) -> None:
    data = {"worker_pid": worker_pid, "backend_pid": backend_pid, "executable": executable}
    path.write_text(json.dumps(data))


def test_orphan_with_matching_binary_is_stopped(tmp_path: Path):
    sleeper = spawn_detached_sleep()
    pid_file = tmp_path / "worker.sock.pid"
    write_pid_file(pid_file, sleeper, finished_pid(), shutil.which("sleep") or "/bin/sleep")
    WorkerPidFile(pid_file).stop_orphan()
    assert wait_dead(sleeper, 1)
    assert not pid_file.exists()


def test_reused_pid_is_not_killed(tmp_path: Path):
    """PID из файла теперь у другой программы: её не трогаем, файл удаляем."""
    sleeper = spawn_detached_sleep()
    pid_file = tmp_path / "worker.sock.pid"
    write_pid_file(pid_file, sleeper, finished_pid(), "/opt/synex/synex_worker")
    try:
        WorkerPidFile(pid_file).stop_orphan()
        assert is_alive(sleeper)
        assert not pid_file.exists()
    finally:
        os.kill(sleeper, 9)


def test_worker_of_running_backend_is_not_killed(tmp_path: Path):
    """Другой backend ещё работает: его воркер не сирота."""
    backend = spawn_detached_sleep()
    sleeper = spawn_detached_sleep()
    pid_file = tmp_path / "worker.sock.pid"
    write_pid_file(pid_file, sleeper, backend, shutil.which("sleep") or "/bin/sleep")
    try:
        WorkerPidFile(pid_file).stop_orphan()
        assert is_alive(sleeper)
        assert pid_file.exists()
    finally:
        os.kill(sleeper, 9)
        os.kill(backend, 9)


def test_broken_pid_file_is_ignored(tmp_path: Path):
    pid_file = tmp_path / "worker.sock.pid"
    pid_file.write_text("garbage")
    WorkerPidFile(pid_file).stop_orphan()
    assert not pid_file.exists()


def test_pid_file_of_another_worker_is_kept(tmp_path: Path):
    pid_file = WorkerPidFile(tmp_path / "worker.sock.pid")
    pid_file.write(123, sys.executable)
    pid_file.clear(456)  # наш воркер был 456, а файл уже про 123
    assert pid_file.read()["worker_pid"] == 123
    pid_file.clear(123)
    assert pid_file.read() is None
