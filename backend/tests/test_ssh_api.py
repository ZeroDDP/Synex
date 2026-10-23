from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.api.deps import get_worker_client
from app.api.router import build_api_router
from app.core.errors import register_error_handlers
from app.keystore import get_keystore
from app.plugins.manager import PluginManager
from app.plugins.ssh.plugin import on_worker_status
from app.plugins.ssh.session import ssh_state
from app.worker.client import WorkerClient
from app.worker.manager import STATUS_WORKER_EXITED, WORKER_STATUS_EVENT
from tests.conftest import start_fake_worker, unique_pipe_name
from tests.fake_worker import FakeWorkerState

# Абсолютный на любой ОС путь к несуществующему файлу (на Windows "/x/y" не абсолютный)
MISSING_FILE = str(Path(__file__).resolve().with_name("definitely-missing") / "id_ed25519")


@pytest.fixture(autouse=True)
def reset_ssh_state():
    ssh_state.mark_disconnected()
    yield
    ssh_state.mark_disconnected()


@pytest.fixture
def key_file(tmp_path: Path) -> str:
    key = tmp_path / "id_ed25519"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n")
    return str(key)


@pytest.fixture
def connect_body(key_file: str) -> dict:
    return {"host": "10.0.0.1", "port": 22, "username": "root", "key_path": key_file}


async def make_api(port: int, missing_socket_path: str):
    worker = WorkerClient(
        pipe_name=unique_pipe_name(),
        socket_path=missing_socket_path,
        tcp_port=port,
        request_timeout=5,
    )
    await worker.connect(timeout=3.0, prefer="tcp")

    app = FastAPI()
    register_error_handlers(app)
    api_router = build_api_router()
    PluginManager().discover(api_router, enabled=["ssh"])
    app.include_router(api_router)
    app.dependency_overrides[get_worker_client] = lambda: worker

    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return client, worker


async def test_connect_sends_core_docs_payload(fake_worker, missing_socket_path, connect_body):
    client, worker = await make_api(fake_worker.port, missing_socket_path)
    async with client:
        response = await client.post("/api/ssh/connect", json=connect_body)
        status = (await client.get("/api/ssh/status")).json()
    await worker.disconnect()

    assert response.status_code == 201
    assert response.json() == {
        "status": "connected",
        "message": "Connected successfully",
        "host": "10.0.0.1",
        "port": 22,
        "username": "root",
        "key_id": None,
    }
    assert status == {
        "connected": True,
        "host": "10.0.0.1",
        "port": 22,
        "username": "root",
        "key_id": None,
        "cwd": None,
    }
    request = fake_worker.requests[-1]
    assert request["action"] == "ssh_connect"
    assert request["payload"] == {"connection": connect_body}


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"key_path": "relative/id_ed25519"}, "absolute path"),
        ({"key_path": MISSING_FILE}, "not found"),
        ({"host": "   "}, "must not be empty"),
        ({"port": 70000}, "less than or equal"),
        ({"key_id": "abc"}, "exactly one of key_id or key_path"),
    ],
)
async def test_connect_validates_input(
    fake_worker, missing_socket_path, connect_body, override, message
):
    client, worker = await make_api(fake_worker.port, missing_socket_path)
    async with client:
        response = await client.post("/api/ssh/connect", json={**connect_body, **override})
    await worker.disconnect()

    assert response.status_code == 422
    assert message in str(response.json())
    assert fake_worker.requests == []  # до воркера дело не дошло


async def test_connect_expands_home(fake_worker, missing_socket_path, monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("key")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    client, worker = await make_api(fake_worker.port, missing_socket_path)
    async with client:
        body = {"host": "h", "username": "u", "key_path": "~/.ssh/id_ed25519"}
        response = await client.post("/api/ssh/connect", json=body)
    await worker.disconnect()

    assert response.status_code == 201
    sent = fake_worker.requests[-1]["payload"]["connection"]["key_path"]
    assert Path(sent) == home / ".ssh" / "id_ed25519"


async def test_connect_failure_from_worker(fake_worker, missing_socket_path, connect_body):
    client, worker = await make_api(fake_worker.port, missing_socket_path)
    async with client:
        response = await client.post(
            "/api/ssh/connect", json={**connect_body, "host": "unreachable"}
        )
        status = (await client.get("/api/ssh/status")).json()
    await worker.disconnect()

    assert response.status_code == 502
    assert response.json() == {
        "error": "ssh_connect_failed",
        "detail": "Connection failed: Connection refused",
    }
    assert status["connected"] is False


async def test_connect_when_worker_is_down(fake_worker, missing_socket_path, connect_body):
    client, worker = await make_api(fake_worker.port, missing_socket_path)
    await worker.disconnect()
    async with client:
        response = await client.post("/api/ssh/connect", json=connect_body)

    assert response.status_code == 503
    assert response.json()["error"] == "worker_unavailable"


async def test_exec_before_connect_is_409(fake_worker, missing_socket_path):
    client, worker = await make_api(fake_worker.port, missing_socket_path)
    async with client:
        response = await client.post("/api/ssh/exec", json={"command": "ls"})
    await worker.disconnect()
    assert response.status_code == 409
    assert response.json()["error"] == "ssh_not_connected"
    assert fake_worker.requests == []


async def test_session_lost_in_worker_resets_state(fake_worker, missing_socket_path, connect_body):
    client, worker = await make_api(fake_worker.port, missing_socket_path)
    async with client:
        assert (await client.post("/api/ssh/connect", json=connect_body)).status_code == 201

        fake_worker.connected = False  # например, воркер перезапустили
        response = await client.post("/api/ssh/exec", json={"command": "ls"})
        assert response.status_code == 409
        assert "Not connected" in response.json()["detail"]
        assert (await client.get("/api/ssh/status")).json()["connected"] is False
    await worker.disconnect()


async def test_no_reply_from_worker_is_504(fake_worker, missing_socket_path, connect_body):
    client, worker = await make_api(fake_worker.port, missing_socket_path)
    async with client:
        await client.post("/api/ssh/connect", json=connect_body)
        response = await client.post(
            "/api/ssh/exec", json={"command": "sleep 1", "timeout": 0.1, "track_cwd": False}
        )
        assert response.status_code == 504
        assert response.json()["error"] == "ssh_exec_timeout"
        assert "may still be running" in response.json()["detail"]
        assert ssh_state.is_connected  # сессия в воркере жива
    await worker.disconnect()


async def test_timeout_stops_command_on_server(shell_worker, missing_socket_path, connect_body):
    client, worker = await make_api(shell_worker.port, missing_socket_path)
    async with client:
        await client.post("/api/ssh/connect", json=connect_body)
        await client.post("/api/ssh/exec", json={"command": "cd /tmp"})

        response = await client.post(
            "/api/ssh/exec", json={"command": "echo started; cd /; sleep 30", "timeout": 1}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["timed_out"] is True and body["exit_code"] == 124
        assert body["stdout"] == "started"
        assert body["cwd"] == "/tmp"  # команда не дошла до конца, cwd прежняя
        assert 900 <= body["duration_ms"] < 5000

        # Воркер свободен: следующая команда выполняется сразу
        after = (await client.post("/api/ssh/exec", json={"command": "pwd"})).json()
        assert (after["stdout"], after["timed_out"]) == ("/tmp", False)
    await worker.disconnect()


async def test_commands_run_one_at_a_time(tmp_path: Path, missing_socket_path, connect_body):
    """Таймаут второй команды не тикает, пока первая занимает воркер."""
    if sys.platform == "win32":
        pytest.skip("needs a POSIX shell")
    state = FakeWorkerState(run_shell=True, home=str(tmp_path), sequential=True)
    server = await start_fake_worker(state)
    client, worker = await make_api(state.port, missing_socket_path)
    try:
        async with client:
            await client.post("/api/ssh/connect", json=connect_body)
            slow = client.post("/api/ssh/exec", json={"command": "sleep 1; echo slow"})
            fast = client.post(
                "/api/ssh/exec", json={"command": "echo fast", "timeout": 0.5, "track_cwd": False}
            )
            slow_response, fast_response = await asyncio.gather(slow, fast)
            assert slow_response.json()["stdout"] == "slow"
            assert fast_response.status_code == 200, fast_response.text
            assert fast_response.json()["stdout"] == "fast"
    finally:
        await worker.disconnect()
        state.close_all()
        server.close()
        await server.wait_closed()


async def test_connect_with_stored_key(fake_worker, missing_socket_path):
    keystore = get_keystore()
    record = await keystore.generate_key("prod")
    secret = await keystore.decrypt_private_key(record.id)

    client, worker = await make_api(fake_worker.port, missing_socket_path)
    async with client:
        body = {"host": "10.0.0.1", "username": "root", "key_id": record.id}
        response = await client.post("/api/ssh/connect", json=body)
        status = (await client.get("/api/ssh/status")).json()
    await worker.disconnect()

    assert response.status_code == 201, response.text
    assert response.json()["key_id"] == record.id and status["key_id"] == record.id
    # Воркер получил путь к файлу с расшифрованным ключом...
    assert fake_worker.connect_key_files == [secret]
    sent_path = Path(fake_worker.requests[-1]["payload"]["connection"]["key_path"])
    assert sent_path.parent == keystore.runtime_dir
    # ...и файл удалён сразу после подключения
    assert not sent_path.exists()
    assert list(keystore.runtime_dir.iterdir()) == []


async def test_connect_with_stored_key_failure_cleans_up(fake_worker, missing_socket_path):
    keystore = get_keystore()
    record = await keystore.generate_key("prod")
    client, worker = await make_api(fake_worker.port, missing_socket_path)
    async with client:
        body = {"host": "unreachable", "username": "root", "key_id": record.id}
        response = await client.post("/api/ssh/connect", json=body)
        missing = await client.post(
            "/api/ssh/connect", json={"host": "h", "username": "u", "key_id": "0" * 32}
        )
    await worker.disconnect()

    assert response.status_code == 502
    assert list(keystore.runtime_dir.iterdir()) == []
    assert missing.status_code == 404 and missing.json()["error"] == "key_not_found"


async def test_worker_exit_event_resets_state():
    ssh_state.mark_connected("10.0.0.1", username="root", port=22)
    await on_worker_status(
        {"event_type": WORKER_STATUS_EVENT, "payload": {"status": "connection_lost"}}
    )
    assert ssh_state.is_connected
    await on_worker_status(
        {"event_type": WORKER_STATUS_EVENT, "payload": {"status": STATUS_WORKER_EXITED}}
    )
    assert not ssh_state.is_connected


async def test_cwd_persists_between_commands(shell_worker, missing_socket_path, connect_body):
    client, worker = await make_api(shell_worker.port, missing_socket_path)
    home = str(Path(shell_worker.home).resolve())
    async with client:
        assert (await client.post("/api/ssh/connect", json=connect_body)).status_code == 201

        first = (await client.post("/api/ssh/exec", json={"command": "pwd"})).json()
        assert first["duration_ms"] >= 0
        first.pop("duration_ms")
        assert first == {
            "stdout": home,
            "stderr": "",
            "exit_code": 0,
            "cwd": home,
            "timed_out": False,
        }

        body = {"command": "mkdir -p app && cd app"}
        assert (await client.post("/api/ssh/exec", json=body)).json()["cwd"] == f"{home}/app"

        pwd = (await client.post("/api/ssh/exec", json={"command": "pwd"})).json()
        assert pwd["stdout"] == f"{home}/app"

        failed = (await client.post("/api/ssh/exec", json={"command": "ls nope"})).json()
        assert failed["exit_code"] != 0 and failed["cwd"] == f"{home}/app"

        body = {"command": "pwd", "track_cwd": False}
        raw = (await client.post("/api/ssh/exec", json=body)).json()
        assert raw["stdout"] == home  # без отслеживания команда идёт в home

        assert (await client.get("/api/ssh/status")).json()["cwd"] == f"{home}/app"
    await worker.disconnect()


async def test_exit_code_survives_worker_bug(tmp_path: Path, missing_socket_path, connect_body):
    """Воркер отдаёт exit_code=0 для любой команды, а API всё равно возвращает настоящий."""
    if sys.platform == "win32":
        pytest.skip("needs a POSIX shell")
    home = tmp_path / "home"
    home.mkdir()
    state = FakeWorkerState(run_shell=True, home=str(home), lose_exit_status=True)
    server = await start_fake_worker(state)
    client, worker = await make_api(state.port, missing_socket_path)
    try:
        async with client:
            await client.post("/api/ssh/connect", json=connect_body)

            async def exit_code(command: str, **extra) -> int:
                body = {"command": command, **extra}
                return (await client.post("/api/ssh/exec", json=body)).json()["exit_code"]

            assert await exit_code("false") == 1
            assert await exit_code("ls /definitely/missing") == 2
            assert await exit_code("sh -c 'exit 42'") == 42
            assert await exit_code("true") == 0
            # Без отслеживания cwd маркера нет, остаётся код от воркера
            assert await exit_code("false", track_cwd=False) == 0
    finally:
        await worker.disconnect()
        state.close_all()
        server.close()
        await server.wait_closed()
