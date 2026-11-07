"""Полный цикл: UI → FastAPI → synex_worker → SSH-сервер → ответ в UI.

Backend запущен целиком на uvicorn с настоящим воркером. Клиент ведёт себя как UI:
REST и оба WebSocket (/ws и терминал сессии) с Origin страницы UI. SSH-сервер —
локальный sshd или внешний из SYNEX_TEST_SSH_* (см. conftest.py).
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys
import time

from app.core.config import settings
from tests.conftest import get_free_port
from tests.integration.sshd import PASSPHRASE, SSHTarget
from tests.integration.ui import UI

# Таймаут команды на сервере делает coreutils timeout: на macOS его нет
SERVER_HAS_TIMEOUT = not (sys.platform == "darwin" and not os.environ.get("SYNEX_TEST_SSH_HOST"))


def session_event(action: str, session_id: str):
    return lambda data: data["action"] == action and data["session_id"] == session_id


async def test_terminal_full_cycle(ui: UI, ssh_target: SSHTarget):
    """Сессия открывается через REST, команды идут через WebSocket терминала, UI видит
    вывод, коды возврата и cwd, а закрытие доходит и до терминала, и до /ws."""
    async with ui.events() as events:
        session_id = await ui.open_session(ssh_target.connection())
        opened = await events.wait_for("ssh.session", session_event("opened", session_id))
        assert opened["data"]["host"] == ssh_target.host

        async with ui.terminal(session_id) as (term, hello):
            assert hello["session"]["channel"] == "dedicated"
            assert (await term.next())["data"]["kind"] == "opened"  # запись из буфера

            result = await ui.run_in_terminal(term, "echo hello; echo oops >&2; cd /tmp")
            assert (result["kind"], result["stdout"], result["stderr"]) == (
                "result",
                "hello",
                "oops",
            )
            assert (result["exit_code"], result["cwd"]) == (0, "/tmp")

            assert (await ui.run_in_terminal(term, "pwd"))["stdout"] == "/tmp"
            assert (await ui.run_in_terminal(term, "sh -c 'exit 3'"))["exit_code"] == 3
            whoami = await ui.run_in_terminal(term, "whoami")
            assert whoami["stdout"] == ssh_target.username

            if SERVER_HAS_TIMEOUT:
                started = time.monotonic()
                slow = await ui.run_in_terminal(term, "echo started; sleep 60", timeout=2)
                assert (slow["timed_out"], slow["exit_code"], slow["stdout"]) == (
                    True,
                    124,
                    "started",
                )
                assert time.monotonic() - started < 20
                assert (await ui.run_in_terminal(term, "echo free"))["stdout"] == "free"

            await term.send("close")
            closed = await term.wait_for("closed")
            assert closed["data"] == {"reason": "closed"}

        gone = await events.wait_for("ssh.session", session_event("closed", session_id))
        assert gone["data"]["reason"] == "closed"
    assert (await ui.api.get(f"/api/ssh/sessions/{session_id}")).status_code == 404


async def test_metrics_full_cycle(ui: UI, ssh_target: SSHTarget):
    """Метрики сервера приходят в /ws: один сервер на все его сессии, пропадает,
    когда закрыта последняя."""
    server = f"{ssh_target.host}:{ssh_target.port}"

    def ok_with(count: int):
        def check(data: dict) -> bool:
            found = [s for s in data["servers"] if s["server"] == server]
            return (
                bool(found)
                and found[0]["status"] == "ok"
                and (len(found[0]["session_ids"]) == count)
            )

        return check

    async with ui.events() as events:
        first = await ui.open_session(ssh_target.connection())
        second = await ui.open_session(ssh_target.connection())
        message = await events.wait_for("metrics", ok_with(2), timeout=30)
        (metrics,) = [s for s in message["data"]["servers"] if s["server"] == server]
        assert sorted(metrics["session_ids"]) == sorted([first, second])
        assert 0 <= metrics["cpu"]["percent"] <= 100
        assert 0 < metrics["memory"]["used"] < metrics["memory"]["total"]
        assert metrics["age_ms"] < 3_000
        # Диски (df) приходят вместе с первым замером; корень есть всегда
        root = next(d for d in metrics["disks"] if d["mount"] == "/")
        assert 0 < root["used"] < root["total"] and 0 <= root["percent"] <= 100
        assert isinstance(metrics["gpus"], list)  # [] без NVIDIA

        await ui.api.delete(f"/api/ssh/sessions/{first}")
        await events.wait_for("metrics", ok_with(1), timeout=30)
        await ui.api.delete(f"/api/ssh/sessions/{second}")
        await events.wait_for(
            "metrics", lambda data: all(s["server"] != server for s in data["servers"])
        )


async def test_sessions_run_commands_in_parallel(ui: UI, ssh_target: SSHTarget):
    ids = [await ui.open_session(ssh_target.connection()) for _ in range(2)]
    started = time.monotonic()
    results = await asyncio.gather(
        *[ui.exec(session_id, f"sleep 1; echo {i}") for i, session_id in enumerate(ids)]
    )
    elapsed = time.monotonic() - started
    assert [r["stdout"] for r in results] == ["0", "1"]
    assert elapsed < 1.9, f"sessions ran one after another ({elapsed:.1f}s)"


async def test_authentication_variants(ui: UI, ssh_target: SSHTarget):
    """Ключ с парольной фразой, ключ из хранилища, пароль; ошибки входа доходят до UI."""
    if ssh_target.encrypted_key_path:
        body = ssh_target.connection(key_path=ssh_target.encrypted_key_path, passphrase=PASSPHRASE)
        session_id = await ui.open_session(body)
        assert (await ui.exec(session_id, "echo enc"))["stdout"] == "enc"

        wrong = await ui.api.post(
            "/api/ssh/sessions",
            json=ssh_target.connection(key_path=ssh_target.encrypted_key_path, passphrase="no"),
        )
        assert wrong.status_code == 502, wrong.text
        assert wrong.json()["error"] == "ssh_connect_failed"

        # В хранилище ключ лежит расшифрованным: фраза при подключении не нужна
        imported = await ui.api.post(
            "/api/keys",
            json={
                "name": "integration",
                "private_key_path": ssh_target.encrypted_key_path,
                "passphrase": PASSPHRASE,
            },
        )
        assert imported.status_code == 201, imported.text
        by_id = await ui.open_session(ssh_target.connection(key_id=imported.json()["id"]))
        assert (await ui.exec(by_id, "whoami"))["stdout"] == ssh_target.username

    if ssh_target.password:
        by_password = await ui.open_session(ssh_target.connection(password=ssh_target.password))
        assert (await ui.exec(by_password, "echo pw"))["stdout"] == "pw"

    denied = await ui.api.post(
        "/api/ssh/sessions", json=ssh_target.connection(password="definitely-wrong")
    )
    assert denied.status_code == 502, denied.text
    assert denied.json()["error"] == "ssh_connect_failed"


async def test_connection_errors_reach_ui(ui: UI, ssh_target: SSHTarget):
    closed_port = get_free_port()  # на нём никто не слушает
    body = {**ssh_target.connection(), "host": "127.0.0.1", "port": closed_port}
    refused = await ui.api.post("/api/ssh/sessions", json=body)
    assert refused.status_code == 502, refused.text
    assert refused.json()["error"] == "ssh_connect_failed"
    assert refused.json()["detail"]


async def test_silent_server_gives_connect_timeout(ui: UI, ssh_target: SSHTarget, monkeypatch):
    """Порт открыт, но SSH не отвечает (файрвол, зависший сервер): понятный 504
    ssh_connect_timeout через SSH_CONNECT_TIMEOUT, а не «воркер не ответил»."""
    monkeypatch.setattr(settings, "SSH_CONNECT_TIMEOUT", 2.0)
    with socket.create_server(("127.0.0.1", 0)) as silent:  # принимает TCP и молчит
        body = {**ssh_target.connection(), "host": "127.0.0.1", "port": silent.getsockname()[1]}
        started = time.monotonic()
        response = await ui.api.post("/api/ssh/sessions", json=body)
        assert response.status_code == 504, response.text
        assert response.json()["error"] == "ssh_connect_timeout"
        assert time.monotonic() - started < 10
    # Воркер после этого работает как обычно
    session_id = await ui.open_session(ssh_target.connection())
    assert (await ui.exec(session_id, "echo alive"))["stdout"] == "alive"


async def test_removed_cwd_does_not_run_command_in_home(ui: UI, ssh_target: SSHTarget):
    """Папку сессии удалили из другой сессии: команда не выполняется (иначе `rm` сработал
    бы в домашней папке), UI получает код 1 и объяснение, сессия переходит в home."""
    first = await ui.open_session(ssh_target.connection())
    second = await ui.open_session(ssh_target.connection())
    directory = f"/tmp/synex-it-{os.getpid()}-{time.monotonic_ns()}"
    assert (await ui.exec(first, f"mkdir -p {directory} && cd {directory}"))["cwd"] == directory
    await ui.exec(second, f"rm -rf {directory}")

    result = await ui.exec(first, "touch must-not-exist")
    assert result["exit_code"] == 1
    assert "is not available" in result["stderr"]
    home = (await ui.exec(second, "cd ~ && pwd"))["stdout"]
    assert result["cwd"] == home
    assert (await ui.exec(second, "ls must-not-exist"))["exit_code"] != 0


async def test_command_does_not_wait_for_input(ui: UI, ssh_target: SSHTarget):
    """Команда, которая читает ввод (cat, read, sudo с паролем), сразу получает конец
    ввода и не держит сессию до таймаута."""
    session_id = await ui.open_session(ssh_target.connection())
    started = time.monotonic()
    result = await ui.exec(session_id, "cat; read x || echo no-input", timeout=30)
    assert (result["stdout"], result["exit_code"], result["timed_out"]) == ("no-input", 0, False)
    assert time.monotonic() - started < 10


async def test_terminal_reconnect_replays_missed_output(ui: UI, ssh_target: SSHTarget):
    """UI отключился (сеть, перезагрузка вкладки): команды выполняются дальше, после
    переподключения с since UI получает всё пропущенное по порядку."""
    session_id = await ui.open_session(ssh_target.connection())
    async with ui.terminal(session_id) as (_, hello):
        last_seen = hello["replay"]["last_seq"]

    await ui.exec(session_id, "echo one")
    await ui.exec(session_id, "echo two")

    async with ui.terminal(session_id, since=last_seen) as (term, hello):
        assert (hello["replay"]["count"], hello["replay"]["gap"]) == (4, False)
        missed = [(await term.next())["data"] for _ in range(4)]
    assert [m["kind"] for m in missed] == ["command", "result", "command", "result"]
    assert [m["stdout"] for m in missed if m["kind"] == "result"] == ["one", "two"]
    assert [m["seq"] for m in missed] == list(range(last_seen + 1, last_seen + 5))


async def test_worker_crash_is_reported_and_recovered(ui: UI, ssh_target: SSHTarget):
    """Воркер упал: терминал и /ws узнают, что сессия потеряна, backend перезапускает
    воркер, и новая сессия работает."""
    async with ui.events() as events:
        session_id = await ui.open_session(ssh_target.connection())
        async with ui.terminal(session_id) as (term, _):
            health = (await ui.api.get("/api/health")).json()
            os.kill(health["worker"]["worker_pid"], getattr(signal, "SIGKILL", signal.SIGTERM))

            closed = await term.wait_for("closed", timeout=30)
            assert closed["data"]["reason"] == "worker_connection_lost"

        # Порядок этих событий не задан: воркер мог перезапуститься раньше, чем
        # закрылась сессия
        await events.wait_for_all(
            [
                ("ssh.session", session_event("closed", session_id)),
                ("worker.status", lambda d: d["status"] == "worker_exited"),
                ("worker.status", lambda d: d["status"] == "connected"),
            ],
            timeout=60,
        )

    health = (await ui.api.get("/api/health")).json()
    assert health["worker"]["worker_restarts"] == 1
    fresh = await ui.open_session(ssh_target.connection())
    assert (await ui.exec(fresh, "echo back"))["stdout"] == "back"
