from __future__ import annotations

import asyncio
import sys
import time

import pytest

import app.worker.manager as manager_module
from app.core.config import settings
from app.core.errors import WorkerCommandError, WorkerConnectionError
from app.core.event_bus import event_bus
from app.worker.manager import (
    STATUS_CONNECTED,
    STATUS_WORKER_EXITED,
    STATUS_WORKER_STARTED,
    WORKER_STATUS_EVENT,
    WorkerManager,
    sanitize_worker_line,
    worker_line_level,
)
from tests.conftest import FAKE_WORKER_SCRIPT
from tests.fake_worker import PASSWORD

# Строки настоящего synex_worker (сборка 2026-11-03), как они приходят из пайпа
ANSI_INFO = (
    "\x1b[2m2026-11-03T11:18:26.840998Z\x1b[0m \x1b[32m INFO\x1b[0m "
    "\x1b[2msynex_worker\x1b[0m\x1b[2m:\x1b[0m "
)
ANSI_ERROR = ANSI_INFO.replace("\x1b[32m INFO", "\x1b[31mERROR")


@pytest.fixture
def events():
    received: list[dict] = []

    async def collect(event: dict) -> None:
        received.append(event["payload"])

    event_bus.subscribe(WORKER_STATUS_EVENT, collect)
    yield received
    event_bus.unsubscribe(WORKER_STATUS_EVENT, collect)


def fake_manager(monkeypatch) -> WorkerManager:
    manager = WorkerManager()
    monkeypatch.setattr(
        manager, "_worker_command", lambda: [sys.executable, str(FAKE_WORKER_SCRIPT)]
    )
    return manager


async def wait_until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


async def test_start_connect_stop(monkeypatch, worker_settings, events):
    manager = fake_manager(monkeypatch)
    await manager.start()
    try:
        status = manager.status()
        assert status["connected"] is True
        assert status["transport"] == "tcp"  # по строке "Listening on tcp ..." от воркера
        assert status["worker_pid"]
        # Вывод воркера читает отдельный поток: под нагрузкой строка может дойти до лога
        # чуть позже, чем клиент успел подключиться
        await wait_until(
            lambda: any("Listening on tcp" in line for line in manager.worker_output_tail)
        )
        assert {"status": STATUS_CONNECTED, "transport": "tcp"} in events

        with pytest.raises(WorkerCommandError, match="Session dont founded"):
            await manager.client.request("ssh_exec", {"command": "ls"})
        process = manager._process
    finally:
        await manager.stop()

    assert process.poll() is not None
    assert manager.status()["worker_pid"] is None


async def test_worker_output_is_drained(monkeypatch, worker_settings):
    # 400 КБ в stdout и stderr, в разы больше буфера пайпа (64 КБ Linux, ~4 КБ Windows).
    # Если пайпы не читать, воркер повиснет на записи и перестанет отвечать.
    monkeypatch.setenv("FAKE_WORKER_SPAM", str(400_000))
    manager = fake_manager(monkeypatch)
    await manager.start()
    try:
        result = await manager.client.request("test_sleep", {"seconds": 0, "echo": "alive"})
        assert result["stdout"] == "alive"
    finally:
        await manager.stop()


async def test_logging_failure_does_not_stop_output_draining(monkeypatch, worker_settings):
    """Если лог не может записать строку (например, эмодзи в stdout с кодировкой cp1251),
    чтение вывода воркера не должно останавливаться: иначе буфер пайпа переполнится."""

    class BrokenLogger:
        """Ломается только на строках вывода воркера (вызовы с line=...)."""

        def __getattr__(self, name):
            def log(*args, **kwargs):
                if "line" in kwargs:
                    raise UnicodeEncodeError("charmap", "🔌", 0, 1, "character maps to <undefined>")

            return log

    monkeypatch.setattr(manager_module, "logger", BrokenLogger())
    monkeypatch.setenv("FAKE_WORKER_SPAM", str(400_000))
    manager = fake_manager(monkeypatch)
    await manager.start()
    try:
        result = await manager.client.request("test_sleep", {"seconds": 0, "echo": "alive"})
        assert result["stdout"] == "alive"
        # Весь спам прочитан до конца, хотя каждая запись в лог падала
        assert all("worker log line" in line for line in manager.worker_output_tail[-10:])
    finally:
        await manager.stop()


async def test_crashed_worker_is_restarted(monkeypatch, worker_settings, events):
    manager = fake_manager(monkeypatch)
    await manager.start()
    try:
        first_pid = manager.status()["worker_pid"]
        with pytest.raises(WorkerConnectionError):
            await manager.client.request("test_crash")

        await wait_until(lambda: any(e["status"] == STATUS_WORKER_EXITED for e in events))
        exited = next(e for e in events if e["status"] == STATUS_WORKER_EXITED)
        assert exited["returncode"] == 3

        await wait_until(lambda: manager.status()["connected"], timeout=15)
        assert manager.status()["worker_pid"] != first_pid
        assert manager.status()["worker_restarts"] == 1
        assert any(e["status"] == STATUS_WORKER_STARTED for e in events)

        result = await manager.client.request("test_sleep", {"seconds": 0, "echo": "back"})
        assert result["stdout"] == "back"
    finally:
        await manager.stop()


async def test_worker_with_unreachable_ipc_is_restarted(monkeypatch, worker_settings, events):
    monkeypatch.setenv("FAKE_WORKER_ACCEPT_ONCE", "1")
    monkeypatch.setattr(manager_module, "SUPERVISE_INTERVAL", 0.2)
    monkeypatch.setattr(manager_module, "RECONNECT_TIMEOUT", 0.3)
    monkeypatch.setattr(manager_module, "MAX_FAILED_RECONNECTS", 2)
    manager = fake_manager(monkeypatch)
    await manager.start()
    try:
        first_pid = manager.status()["worker_pid"]
        # Воркер рвёт соединение и больше не принимает новые: процесс жив, IPC мёртв
        with pytest.raises(WorkerConnectionError):
            await manager.client.request("test_close")

        await wait_until(lambda: manager.status()["worker_restarts"] == 1, timeout=15)
        await wait_until(lambda: manager.status()["connected"], timeout=10)
        assert manager.status()["worker_pid"] != first_pid
        result = await manager.client.request("test_sleep", {"seconds": 0, "echo": "fresh"})
        assert result["stdout"] == "fresh"
    finally:
        await manager.stop()


async def test_missing_executable_does_not_crash_startup(monkeypatch, worker_settings, tmp_path):
    monkeypatch.setattr(settings, "RUST_EXECUTABLE_PATH_WIN32", str(tmp_path / "nope.exe"))
    monkeypatch.setattr(settings, "RUST_EXECUTABLE_PATH_UNIX", str(tmp_path / "nope"))
    monkeypatch.setattr(settings, "RUST_IPC_CONNECT_TIMEOUT", 0.5)
    manager = WorkerManager()
    await manager.start()
    try:
        assert manager.status()["connected"] is False
        assert manager.status()["worker_pid"] is None
        with pytest.raises(WorkerConnectionError, match="Not connected"):
            await manager.client.request("ssh_exec", {"command": "ls"})
    finally:
        await manager.stop()


def test_relative_executable_path_is_resolved_from_backend_dir(monkeypatch):
    monkeypatch.setattr(settings, "RUST_EXECUTABLE_PATH_WIN32", "./app/worker.exe")
    monkeypatch.setattr(settings, "RUST_EXECUTABLE_PATH_UNIX", "./app/worker")
    first = WorkerManager.executable_candidates()[0]
    assert first.parent == settings.BASE_DIR / "app"


def test_worker_output_is_sanitized():
    line = sanitize_worker_line(
        ANSI_INFO + "Попытка подключения к root@1.2.3.4:22 (key: None, passphrase: p4ss, "
        "password: hunter2, with comma)...\r\n"
    )
    assert line == (
        "2026-11-03T11:18:26.840998Z  INFO synex_worker: Попытка подключения к "
        "root@1.2.3.4:22 (key: None, passphrase: <hidden>"
    )
    assert sanitize_worker_line("Connected successfully") == "Connected successfully"


@pytest.mark.parametrize(
    ("line", "level"),
    [
        ("🔌 Listening on pipe: \\\\.\\pipe\\synex_worker", "info"),
        ("🔌 Listening on tcp 127.0.0.1:9100", "info"),
        (ANSI_INFO + "Выполняется команда: grep error /var/log/syslog", "debug"),
        (ANSI_ERROR + "Session dont founded by key: s1", "warning"),
        ("Pipe failed (Invalid name. (os error 123)), falling back to TCP", "warning"),
        ("❌ Неизвестное действие: nope", "warning"),
        ("thread 'main' panicked at src/main.rs:10:5", "error"),
        ("  echo failed; exit 1", "debug"),  # продолжение многострочной команды
    ],
)
def test_worker_output_levels(line, level):
    assert worker_line_level(sanitize_worker_line(line)) == level


async def test_password_never_reaches_output_tail(monkeypatch, worker_settings):
    manager = fake_manager(monkeypatch)
    await manager.start()
    try:
        connection = {"host": "h", "port": 22, "username": "u", "password": PASSWORD}
        await manager.client.request("ssh_connect", {"connection": connection}, request_id="s1")
        await wait_until(lambda: any("Попытка" in line for line in manager.worker_output_tail))
        tail = "\n".join(manager.worker_output_tail)
        assert PASSWORD not in tail
        assert "password: <hidden>" in tail or "passphrase: <hidden>" in tail
        assert "\x1b[" not in tail
    finally:
        await manager.stop()
