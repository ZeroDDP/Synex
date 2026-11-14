"""Тесты с настоящим synex_worker на уровне его протокола и API backend.

Где берётся воркер, см. tests/real_worker.py: сборка для этой ОС (Windows: exe из
репозитория; Linux/macOS: synex_worker, собранный cargo) или команда из
SYNEX_TEST_WORKER (например, exe через wine).

SSH-сценарии включаются переменными окружения:
    SYNEX_TEST_SSH_HOST, SYNEX_TEST_SSH_USER, SYNEX_TEST_SSH_KEY, [SYNEX_TEST_SSH_PORT]
    SYNEX_TEST_SSH_PASSWORD      дополнительно проверить вход по паролю
Воркер (с 2026-11-14) подключается только с отпечатком ключа сервера (TOFU): тесты, как
UI, узнают его первым подключением (host_key_unknown) и подключаются с ним. Проверку в
воркере никто не выключает.
Полный цикл UI → FastAPI → воркер → SSH-сервер с локальным sshd: tests/integration/.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import httpx
import pytest
from websockets.asyncio.client import connect

from app.core.app import create_app
from app.core.errors import WorkerCommandError
from app.plugins.ssh.wrapper import build_command, new_marker, parse_output
from app.worker.manager import WorkerManager
from tests import real_worker as rw
from tests.ssh_helpers import recv_json, send_json, serve

pytestmark = pytest.mark.skipif(not rw.AVAILABLE, reason=rw.SKIP_REASON)

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


async def tofu_connect(client, request_id: str, connection: dict, timeout: float = 30) -> dict:
    """ssh_connect так, как его делает backend после подтверждения ключа пользователем:
    первый вызов без отпечатка отвечает host_key_unknown с payload.fingerprint, второй
    идёт с ним. Сборки воркера без проверки ключа подключаются с первого раза."""
    try:
        return await client.request(
            "ssh_connect", {"connection": connection}, timeout=timeout, request_id=request_id
        )
    except WorkerCommandError as e:
        if not e.stderr.startswith("host_key_unknown"):
            raise
        assert e.payload["fingerprint"].startswith("SHA256:"), e.payload
        assert e.payload["key_type"], e.payload
        fingerprint = e.payload["fingerprint"]
    return await client.request(
        "ssh_connect",
        {"connection": {**connection, "host_key_fingerprint": fingerprint}},
        timeout=timeout,
        request_id=request_id,
    )


async def trust_server(api: httpx.AsyncClient) -> None:
    """Подтвердить ключ SSH-сервера в backend, как пользователь в UI (409 → known-hosts)."""
    first = await api.post("/api/ssh/sessions", json=ssh_connection())
    if first.status_code == 201:  # воркер без проверки ключа
        await api.delete(f"/api/ssh/sessions/{first.json()['session_id']}")
        return
    assert first.json()["error"] == "ssh_host_key_unknown", first.text
    host_key = first.json()["host_key"]
    trusted = await api.post(
        "/api/ssh/known-hosts",
        json={key: host_key[key] for key in ("host", "port", "fingerprint", "key_type")},
    )
    assert trusted.status_code == 201, trusted.text


@pytest.fixture
def real_worker_settings(monkeypatch):
    with rw.real_worker_settings(monkeypatch):
        yield


@pytest.fixture
async def real_worker(real_worker_settings):
    manager = WorkerManager()
    await manager.start()
    yield manager
    await manager.stop()


async def test_real_worker_roundtrip(real_worker):
    status = real_worker.status()
    assert status["connected"], real_worker.worker_output_tail
    assert status["transport"] == rw.EXPECTED_TRANSPORT, status

    client = real_worker.client
    with pytest.raises(WorkerCommandError, match="Session dont founded"):
        await client.request("ssh_exec", {"command": "whoami"}, request_id="no-such-session")

    with pytest.raises(WorkerCommandError, match="Unknown action"):
        await client.request("docker_list", {"path": "/"}, timeout=5)
    # SFTP-действия (сборка 2026-11-12) требуют сессию; в старой сборке их нет вовсе
    with pytest.raises(WorkerCommandError, match="Session dont founded|Unknown action"):
        await client.request("sftp_list_dir", {"remote_path": "/"}, request_id="no-session")

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
    connected = await tofu_connect(client, "s1", ssh_connection())
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
    await tofu_connect(ssh_client, "s2", ssh_connection())
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


async def test_real_worker_raw_exit_codes(ssh_client):
    """Коды возврата от самого воркера, без обёртки Python. Сборки до 2026-11-14 теряли
    exit-status (всегда 0): для них тест xfail. Вывод показывает, где код теряется."""
    probe = await ssh_client.request("ssh_exec", {"command": "exit 3"}, timeout=30, request_id="s1")
    if probe["exit_code"] == 0:
        pytest.xfail("this synex_worker build loses the exit status (before 2026-11-14)")
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
            await tofu_connect(channel, f"p{i}", ssh_connection())
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
            await trust_server(api)
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
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as api:
            await trust_server(api)
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
            transport=transport, base_url="http://127.0.0.1", timeout=60
        ) as api:
            await trust_server(api)
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
