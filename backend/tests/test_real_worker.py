"""Тесты с настоящим synex_worker.

По умолчанию запускаются на Windows, если exe лежит по пути из настроек. На Linux/macOS
воркер можно запустить командой из SYNEX_TEST_WORKER (например, скриптом, который
запускает exe через wine): тогда pipe заведомо не создаётся и воркер уходит на TCP.

SSH-сценарии включаются переменными окружения:
    SYNEX_TEST_SSH_HOST, SYNEX_TEST_SSH_USER, SYNEX_TEST_SSH_KEY, [SYNEX_TEST_SSH_PORT]
    SYNEX_TEST_SSH_PASSWORD      дополнительно проверить вход по паролю
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import httpx
import pytest
from websockets.asyncio.client import connect

from app.core.app import create_app
from app.core.config import settings
from app.core.errors import WorkerCommandError
from app.plugins.ssh.wrapper import build_command, new_marker, parse_output
from app.worker import manager as manager_module
from app.worker.manager import WorkerManager
from tests.conftest import unique_pipe_name
from tests.ssh_helpers import recv_json, send_json, serve

WORKER_COMMAND = os.environ.get("SYNEX_TEST_WORKER")
EXE = next((p for p in WorkerManager.executable_candidates() if p.is_file()), None)
ON_WINDOWS = sys.platform == "win32" and EXE is not None and not WORKER_COMMAND

pytestmark = pytest.mark.skipif(
    not (ON_WINDOWS or WORKER_COMMAND),
    reason="needs Windows and synex_worker.exe, or SYNEX_TEST_WORKER",
)

needs_ssh = pytest.mark.skipif(
    not os.environ.get("SYNEX_TEST_SSH_HOST"), reason="SYNEX_TEST_SSH_* not set"
)


def ssh_connection(**auth) -> dict:
    connection = {
        "host": os.environ["SYNEX_TEST_SSH_HOST"],
        "port": int(os.environ.get("SYNEX_TEST_SSH_PORT", "22")),
        "username": os.environ["SYNEX_TEST_SSH_USER"],
    }
    connection.update(auth or {"key_path": os.environ["SYNEX_TEST_SSH_KEY"]})
    return connection


@pytest.fixture
def real_worker_settings(monkeypatch, free_port):
    # Своё имя pipe и порт, чтобы не столкнуться с запущенным dev-бэкендом
    pipe = unique_pipe_name() if ON_WINDOWS else "no-pipe-use-tcp"  # wine: сразу TCP
    monkeypatch.setattr(settings, "RUST_IPC_PIPE_NAME", pipe)
    monkeypatch.setattr(settings, "RUST_IPC_TCP_FALLBACK_PORT", free_port)
    monkeypatch.setattr(settings, "RUST_IPC_REQUEST_TIMEOUT", 10.0)
    monkeypatch.setattr(settings, "RUST_IPC_CONNECT_TIMEOUT", 30.0)  # wine стартует долго
    if WORKER_COMMAND:
        monkeypatch.setattr(
            manager_module.WorkerManager, "_worker_command", lambda self: [WORKER_COMMAND]
        )


@pytest.fixture
async def real_worker(real_worker_settings):
    manager = WorkerManager()
    await manager.start()
    yield manager
    await manager.stop()


async def test_real_worker_roundtrip(real_worker):
    status = real_worker.status()
    assert status["connected"], real_worker.worker_output_tail
    assert status["transport"] == ("pipe" if ON_WINDOWS else "tcp"), status

    client = real_worker.client
    with pytest.raises(WorkerCommandError, match="Session dont founded"):
        await client.request("ssh_exec", {"command": "whoami"}, request_id="no-such-session")

    with pytest.raises(WorkerCommandError, match="Unknown action"):
        await client.request("sftp_list_dir", {"path": "/"}, timeout=5)

    with pytest.raises(WorkerCommandError, match="Session not found"):
        await client.request("ssh_disconnect", {}, request_id="no-such-session")

    # Несколько запросов одновременно по одному подключению
    results = await asyncio.gather(
        *[client.request("ssh_exec", {"command": f"echo {i}"}) for i in range(10)],
        return_exceptions=True,
    )
    assert all(isinstance(r, WorkerCommandError) for r in results), results
    assert real_worker.status()["connected"]


@pytest.fixture
async def ssh_client(real_worker):
    if not os.environ.get("SYNEX_TEST_SSH_HOST"):
        pytest.skip("SYNEX_TEST_SSH_* not set")
    client = real_worker.client
    connected = await client.request(
        "ssh_connect", {"connection": ssh_connection()}, timeout=30, request_id="s1"
    )
    assert connected["stdout"] == "Connected successfully, id: s1"
    return client


async def test_real_ssh_session_with_cwd(ssh_client):
    marker = new_marker()
    cwd: str | None = None

    async def run(command: str) -> dict:
        nonlocal cwd
        result = await ssh_client.request(
            "ssh_exec",
            {"command": build_command(command, cwd, marker, timeout=20)},
            timeout=40,
            request_id="s1",
        )
        parsed = parse_output(result["stdout"], marker, timeout_applied=True)
        cwd = parsed.cwd or cwd
        result["stdout"] = parsed.stdout
        if parsed.exit_code is not None:
            result["exit_code"] = parsed.exit_code  # так же делает плагин SSH
        return result

    home = (await run("pwd"))["stdout"]
    assert home.startswith("/") and cwd == home

    await run("cd /tmp")
    assert cwd == "/tmp"
    assert (await run("pwd"))["stdout"] == "/tmp"
    assert (await run("false"))["exit_code"] == 1
    assert (await run("ls /definitely/missing"))["exit_code"] == 2
    assert (await run("echo hi; exit 0"))["exit_code"] == 0
    assert cwd == "/tmp"


async def test_real_sessions_are_request_ids(ssh_client):
    """Сессия воркера = id запроса; ssh_disconnect закрывает только свою."""
    await ssh_client.request(
        "ssh_connect", {"connection": ssh_connection()}, timeout=30, request_id="s2"
    )
    first = await ssh_client.request("ssh_exec", {"command": "echo one"}, request_id="s1")
    second = await ssh_client.request("ssh_exec", {"command": "echo two"}, request_id="s2")
    assert (first["stdout"].strip(), second["stdout"].strip()) == ("one", "two")

    closed = await ssh_client.request("ssh_disconnect", {"session_id": "s2"}, request_id="s2")
    assert closed["stdout"] == "Disconnected id: s2"
    with pytest.raises(WorkerCommandError, match="Session dont founded"):
        await ssh_client.request("ssh_exec", {"command": "echo two"}, request_id="s2")
    assert await ssh_client.request("ssh_exec", {"command": "echo"}, request_id="s1")


async def test_real_same_session_replies_in_order(ssh_client):
    """Клиент сопоставляет ответы с одинаковым id по порядку: воркер должен его сохранять."""
    slow, fast = await asyncio.gather(
        ssh_client.request("ssh_exec", {"command": "sleep 1; echo slow"}, request_id="s1"),
        ssh_client.request("ssh_exec", {"command": "echo fast"}, request_id="s1"),
    )
    assert (slow["stdout"].strip(), fast["stdout"].strip()) == ("slow", "fast")


async def test_real_monitor_get_values(ssh_client):
    result = await ssh_client.request("monitor_get_values", {}, request_id="s1")
    values = json.loads(result["stdout"])
    assert values["ok"] is True, values
    assert 0 <= values["cpu_usage_percent"] <= 100
    assert 0 < values["mem_used_kb"] <= values["mem_total_kb"]
    assert 0 <= values["mem_usage_percent"] <= 100


@pytest.mark.xfail(reason="synex_worker возвращает exit_code=0 (сборка 2026-11-03)", strict=False)
async def test_real_worker_raw_exit_codes(ssh_client):
    """Коды возврата от самого воркера, без обёртки Python.

    Пока воркер теряет exit-status, тест помечен xfail. Когда баг исправят, pytest
    покажет XPASS: тогда отметку можно снять. Вывод показывает, где код теряется.
    """
    cases = {
        "true": 0,
        "false": 1,
        "exit 3": 3,
        "ls /definitely/missing": 2,
        "echo out; exit 5": 5,
        "sleep 1; exit 4": 4,
    }
    got = {}
    for command in cases:
        result = await ssh_client.request(
            "ssh_exec", {"command": command}, timeout=30, request_id="s1"
        )
        got[command] = result["exit_code"]

    report = "\n".join(
        f"  {'OK ' if got[c] == want else 'BAD'} {c!r}: expected {want}, worker returned {got[c]}"
        for c, want in cases.items()
    )
    assert got == cases, f"synex_worker returns wrong exit codes:\n{report}"


@needs_ssh
async def test_real_extra_connections_run_in_parallel(real_worker):
    """Сессии в разных IPC-подключениях выполняют команды параллельно. На Windows это
    заодно проверка, что Named Pipe принимает несколько подключений сразу."""
    channels = [await real_worker.open_channel() for _ in range(2)]
    try:
        assert all(c.transport == real_worker.client.transport for c in channels)
        for i, channel in enumerate(channels):
            await channel.request(
                "ssh_connect", {"connection": ssh_connection()}, timeout=30, request_id=f"p{i}"
            )
        started = time.monotonic()
        results = await asyncio.gather(
            *[
                channel.request("ssh_exec", {"command": f"sleep 1; echo {i}"}, request_id=f"p{i}")
                for i, channel in enumerate(channels)
            ]
        )
        elapsed = time.monotonic() - started
        assert [r["stdout"].strip() for r in results] == ["0", "1"]
        assert elapsed < 1.8, f"commands ran one after another ({elapsed:.1f}s)"
        assert real_worker.status()["channels"] == 2
    finally:
        for channel in channels:
            await channel.disconnect()


@needs_ssh
async def test_real_terminal_websocket(real_worker_settings):
    """Приложение на uvicorn: сессия через REST, терминал через WebSocket."""
    app = create_app()
    async with serve(app) as base:
        http = base.replace("ws://", "http://")
        async with httpx.AsyncClient(base_url=http, timeout=60) as api:
            created = await api.post("/api/ssh/sessions", json=ssh_connection())
            assert created.status_code == 201, created.text
            info = created.json()
            assert info["channel"] == "dedicated", info
            session_id = info["session_id"]

            url = f"{base}/api/ssh/sessions/{session_id}/ws"
            async with connect(url) as ws:
                hello = await recv_json(ws)
                assert hello["data"]["session"]["session_id"] == session_id
                await recv_json(ws)  # запись opened
                await send_json(ws, "exec", {"command": "cd /tmp && pwd", "id": "c1"})
                command = await recv_json(ws, timeout=30)
                result = await recv_json(ws, timeout=30)
                assert command["data"]["kind"] == "command"
                assert (result["data"]["stdout"], result["data"]["request_id"]) == ("/tmp", "c1")

                closed = await api.delete(f"/api/ssh/sessions/{session_id}")
                assert closed.status_code == 200, closed.text
                assert (await recv_json(ws))["data"] == {"reason": "closed"}


@needs_ssh
async def test_real_monitoring(real_worker_settings):
    """Плагин monitor опрашивает настоящий воркер: сервер один, сколько бы сессий ни было."""
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
            ids = []
            for _ in range(2):
                created = await api.post("/api/ssh/sessions", json=ssh_connection())
                assert created.status_code == 201, created.text
                ids.append(created.json()["session_id"])

            async with asyncio.timeout(15):
                while True:
                    snapshot = (await api.get("/api/monitor/metrics")).json()
                    (server,) = snapshot["servers"]
                    if server["status"] == "ok":
                        break
                    assert server["status"] in ("waiting", "warming_up"), server
                    await asyncio.sleep(0.2)

            assert sorted(server["session_ids"]) == sorted(ids)
            assert 0 <= server["cpu"]["percent"] <= 100
            memory = server["memory"]
            assert 0 < memory["used"] < memory["total"]
            assert 0 < memory["percent"] < 100
            assert server["age_ms"] < 3_000


@needs_ssh
async def test_real_api(real_worker_settings):
    """Всё приложение целиком: HTTP → плагин ssh → synex_worker → сервер."""
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=60
        ) as api:
            connection = ssh_connection()
            response = await api.post("/api/ssh/connect", json=connection)
            assert response.status_code == 201, response.text
            first = response.json()["session_id"]

            async def run(command: str, **extra) -> dict:
                body = {"command": command, **extra}
                reply = await api.post("/api/ssh/exec", json=body)
                assert reply.status_code == 200, reply.text
                return reply.json()

            assert (await run("cd /tmp"))["cwd"] == "/tmp"
            assert (await run("pwd"))["stdout"] == "/tmp"
            assert (await run("false"))["exit_code"] == 1

            # Таймаут на сервере: команда прервана, воркер сразу свободен
            body = await run("echo started; sleep 60", timeout=2)
            assert body["timed_out"] is True, body
            assert body["stdout"] == "started"
            assert body["duration_ms"] < 15_000
            assert (await run("echo next"))["stdout"] == "next"

            # Вторая сессия к тому же серверу через ключ из хранилища
            imported = await api.post(
                "/api/keys",
                json={"name": "test", "private_key_path": os.environ["SYNEX_TEST_SSH_KEY"]},
            )
            assert imported.status_code == 201, imported.text
            key_id = imported.json()["id"]
            connection.pop("key_path")
            by_id = await api.post("/api/ssh/connect", json={**connection, "key_id": key_id})
            assert by_id.status_code == 201, by_id.text
            second = by_id.json()["session_id"]

            # У каждой сессии своя cwd
            assert (await run("pwd", session_id=second))["stdout"] != "/tmp"
            assert (await run("pwd", session_id=first))["stdout"] == "/tmp"

            closed = await api.post("/api/ssh/disconnect", json={"session_id": second})
            assert closed.status_code == 200, closed.text
            gone = await api.post("/api/ssh/exec", json={"command": "pwd", "session_id": second})
            assert gone.status_code == 404
            assert (await run("echo still-open"))["session_id"] == first

            password = os.environ.get("SYNEX_TEST_SSH_PASSWORD")
            if password:
                by_password = await api.post(
                    "/api/ssh/connect", json={**connection, "password": password}
                )
                assert by_password.status_code == 201, by_password.text
                assert (await run("whoami"))["stdout"] == connection["username"]

            wrong = await api.post(
                "/api/ssh/connect", json={**connection, "password": "definitely-wrong"}
            )
            assert wrong.status_code == 502, wrong.text
            assert wrong.json()["error"] == "ssh_connect_failed"
