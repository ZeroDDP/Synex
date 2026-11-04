from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import httpx
import pytest

from app.keystore import get_keystore
from tests.conftest import start_fake_worker
from tests.fake_worker import PASSPHRASE, PASSWORD, FakeWorkerState
from tests.ssh_helpers import make_api

# Абсолютный на любой ОС путь к несуществующему файлу (на Windows "/x/y" не абсолютный)
MISSING_FILE = str(Path(__file__).resolve().with_name("definitely-missing") / "id_ed25519")


@pytest.fixture
def key_file(tmp_path: Path) -> str:
    key = tmp_path / "id_ed25519"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n")
    return str(key)


@pytest.fixture
def encrypted_key_file(tmp_path: Path) -> str:
    key = tmp_path / "id_encrypted"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nENCRYPTED\n")
    return str(key)


@pytest.fixture
def connect_body(key_file: str) -> dict:
    return {"host": "10.0.0.1", "port": 22, "username": "root", "key_path": key_file}


@pytest.fixture
async def ssh(fake_worker, missing_socket_path):
    client, harness = await make_api(fake_worker.port, missing_socket_path)
    async with client:
        harness.api = client
        yield harness
    await harness.disconnect()


@pytest.fixture
def api(ssh) -> httpx.AsyncClient:
    return ssh.api


# ----------------------------------------------------------------- connect


async def test_connect_opens_session(api, fake_worker, connect_body):
    response = await api.post("/api/ssh/connect", json=connect_body)
    assert response.status_code == 201, response.text
    body = response.json()
    session_id = body.pop("session_id")
    assert re.fullmatch(r"[0-9a-f]{16}", session_id)
    assert body == {
        "status": "connected",
        "message": "Connected successfully",
        "host": "10.0.0.1",
        "port": 22,
        "username": "root",
        "auth": "key_path",
        "key_id": None,
    }

    status = (await api.get("/api/ssh/status")).json()
    assert status.pop("created_at")
    assert status == {
        "connected": True,
        "session_id": session_id,
        "host": "10.0.0.1",
        "port": 22,
        "username": "root",
        "auth": "key_path",
        "key_id": None,
        "cwd": None,
        "sessions": 1,
    }

    # Сессия в воркере называется id запроса
    request = fake_worker.requests[-1]
    assert (request["action"], request["id"]) == ("ssh_connect", session_id)
    assert request["payload"] == {"connection": connect_body}
    assert fake_worker.sessions == {session_id}


async def test_connect_with_password(api, fake_worker):
    body = {"host": "10.0.0.1", "username": "root", "password": PASSWORD}
    response = await api.post("/api/ssh/connect", json=body)
    assert response.status_code == 201, response.text
    assert response.json()["auth"] == "password"
    assert fake_worker.requests[-1]["payload"]["connection"] == {
        "host": "10.0.0.1",
        "port": 22,
        "username": "root",
        "password": PASSWORD,
    }
    status = await api.get("/api/ssh/status")
    assert PASSWORD not in response.text + status.text

    wrong = await api.post("/api/ssh/connect", json={**body, "password": "nope"})
    assert wrong.status_code == 502
    assert wrong.json() == {
        "error": "ssh_connect_failed",
        "detail": "Connection failed: SSH authentication failed: access denied",
    }


async def test_connect_with_passphrase(ssh, api, fake_worker, encrypted_key_file):
    body = {"host": "10.0.0.1", "username": "root", "key_path": encrypted_key_file}

    missing = await api.post("/api/ssh/connect", json=body)
    assert missing.status_code == 502
    assert "The key is encrypted" in missing.json()["detail"]

    wrong = await api.post("/api/ssh/connect", json={**body, "passphrase": "nope"})
    assert wrong.status_code == 502
    assert "cryptographic error" in wrong.json()["detail"]

    ok = await api.post("/api/ssh/connect", json={**body, "passphrase": PASSPHRASE})
    assert ok.status_code == 201, ok.text
    assert ok.json()["auth"] == "key_path"
    assert fake_worker.requests[-1]["payload"]["connection"]["passphrase"] == PASSPHRASE
    assert len(ssh.manager) == 1


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"key_path": "relative/id_ed25519"}, "absolute path"),
        ({"key_path": MISSING_FILE}, "not found"),
        ({"host": "   "}, "must not be empty"),
        ({"port": 70000}, "less than or equal"),
        ({"key_id": "abc"}, "exactly one of key_id, key_path or password"),
        ({"password": "x"}, "exactly one of key_id, key_path or password"),
        ({"key_path": None}, "exactly one of key_id, key_path or password"),
        ({"key_path": None, "key_id": "abc", "passphrase": "x"}, "only used with key_path"),
        ({"key_path": None, "password": "x", "passphrase": "x"}, "only used with key_path"),
        ({"password": ""}, "too_short"),
    ],
)
async def test_connect_validates_input(api, fake_worker, connect_body, override, message):
    response = await api.post("/api/ssh/connect", json={**connect_body, **override})
    assert response.status_code == 422
    assert message in str(response.json())
    assert fake_worker.requests == []  # до воркера дело не дошло


async def test_connect_expands_home(api, fake_worker, monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("key")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    body = {"host": "h", "username": "u", "key_path": "~/.ssh/id_ed25519"}
    response = await api.post("/api/ssh/connect", json=body)

    assert response.status_code == 201
    sent = fake_worker.requests[-1]["payload"]["connection"]["key_path"]
    assert Path(sent) == home / ".ssh" / "id_ed25519"


async def test_connect_failure_from_worker(api, connect_body):
    response = await api.post("/api/ssh/connect", json={**connect_body, "host": "unreachable"})
    status = (await api.get("/api/ssh/status")).json()

    assert response.status_code == 502
    assert response.json() == {
        "error": "ssh_connect_failed",
        "detail": "Connection failed: Connection refused",
    }
    assert status == {"connected": False, **dict.fromkeys(STATUS_FIELDS), "sessions": 0}


STATUS_FIELDS = ("session_id", "host", "port", "username", "auth", "key_id", "cwd", "created_at")


async def test_connect_when_worker_is_down(fake_worker, missing_socket_path, connect_body):
    client, worker = await make_api(fake_worker.port, missing_socket_path)
    await worker.disconnect()
    async with client:
        response = await client.post("/api/ssh/connect", json=connect_body)

    assert response.status_code == 503
    assert response.json()["error"] == "worker_unavailable"


@pytest.mark.parametrize("channels", [True, False], ids=["dedicated", "shared"])
async def test_connect_timeout_closes_late_session(missing_socket_path, connect_body, channels):
    """Воркер довёл подключение до конца уже после таймаута: сессия не должна повиснуть.

    Отдельное подключение закрывается целиком (воркер закроет его сессии сам), а в общем
    подключении уходит ssh_disconnect с тем же id.
    """
    state = FakeWorkerState(connect_delay=0.5)
    server = await start_fake_worker(state)
    client, harness = await make_api(
        state.port, missing_socket_path, request_timeout=0.2, channels=channels
    )
    try:
        async with client:
            response = await client.post("/api/ssh/connect", json=connect_body)
            assert response.status_code == 504
            assert response.json()["error"] == "worker_timeout"
            assert len(harness.manager) == 0

            expected = ["ssh_connect"] if channels else ["ssh_connect", "ssh_disconnect"]
            await asyncio.sleep(0.8)
            assert [r["action"] for r in state.requests] == expected
            assert len({r["id"] for r in state.requests}) == 1
            assert state.sessions == set()
            assert harness.provider.open_channels == 0
    finally:
        await harness.disconnect()
        server.close()
        await server.wait_closed()


# -------------------------------------------------------- several sessions


async def test_several_sessions(api, fake_worker, connect_body):
    first = (await api.post("/api/ssh/connect", json=connect_body)).json()["session_id"]
    second_body = {**connect_body, "host": "10.0.0.2"}
    second = (await api.post("/api/ssh/connect", json=second_body)).json()["session_id"]
    assert first != second
    assert fake_worker.sessions == {first, second}

    async def run(**body) -> dict:
        response = await api.post("/api/ssh/exec", json={"track_cwd": False, **body})
        assert response.status_code == 200, response.text
        return response.json()

    # Без session_id команда идёт в последнюю открытую сессию
    assert (await run(command="hostname"))["session_id"] == second
    assert (await run(command="hostname", session_id=first))["session_id"] == first
    assert [r["id"] for r in fake_worker.requests if r["action"] == "ssh_exec"] == [second, first]

    status = (await api.get("/api/ssh/status", params={"session_id": first})).json()
    assert (status["host"], status["sessions"]) == ("10.0.0.1", 2)

    # Закрываем вторую: текущей снова становится первая
    closed = await api.post("/api/ssh/disconnect", json={"session_id": second})
    assert closed.status_code == 200
    assert closed.json() == {
        "status": "disconnected",
        "session_id": second,
        "message": f"Disconnected id: {second}",
    }
    request = fake_worker.requests[-1]
    assert (request["action"], request["id"]) == ("ssh_disconnect", second)
    assert request["payload"] == {"session_id": second}
    assert fake_worker.sessions == {first}
    assert (await run(command="hostname"))["session_id"] == first
    assert (await api.get("/api/ssh/status")).json()["session_id"] == first


async def test_unknown_session_is_404(api, connect_body):
    await api.post("/api/ssh/connect", json=connect_body)
    for response in (
        await api.post("/api/ssh/exec", json={"command": "ls", "session_id": "missing"}),
        await api.post("/api/ssh/disconnect", json={"session_id": "missing"}),
        await api.get("/api/ssh/status", params={"session_id": "missing"}),
    ):
        assert response.status_code == 404
        assert response.json() == {
            "error": "ssh_session_not_found",
            "detail": "SSH session 'missing' not found",
        }

    bad = await api.post("/api/ssh/exec", json={"command": "ls", "session_id": "../x"})
    assert bad.status_code == 422


async def test_disconnect_without_session_id_closes_current(api, fake_worker, connect_body):
    session_id = (await api.post("/api/ssh/connect", json=connect_body)).json()["session_id"]
    response = await api.post("/api/ssh/disconnect")
    assert response.status_code == 200
    assert response.json()["session_id"] == session_id
    assert fake_worker.sessions == set()

    again = await api.post("/api/ssh/disconnect")
    assert again.status_code == 409
    assert again.json()["error"] == "ssh_not_connected"
    assert (await api.get("/api/ssh/status")).json()["connected"] is False


async def test_disconnect_of_session_lost_in_worker(ssh, api, fake_worker, connect_body):
    session_id = (await api.post("/api/ssh/connect", json=connect_body)).json()["session_id"]
    fake_worker.drop_sessions()
    response = await api.post("/api/ssh/disconnect", json={"session_id": session_id})
    assert response.status_code == 200
    assert response.json()["message"] == "The session was already closed in the worker"
    assert len(ssh.manager) == 0


# -------------------------------------------------------------------- exec


async def test_exec_before_connect_is_409(api, fake_worker):
    response = await api.post("/api/ssh/exec", json={"command": "ls"})
    assert response.status_code == 409
    assert response.json()["error"] == "ssh_not_connected"
    assert fake_worker.requests == []


async def test_session_lost_in_worker_resets_state(api, fake_worker, connect_body):
    session_id = (await api.post("/api/ssh/connect", json=connect_body)).json()["session_id"]

    fake_worker.drop_sessions()  # например, воркер перезапустили
    response = await api.post("/api/ssh/exec", json={"command": "ls"})
    assert response.status_code == 409
    assert response.json()["detail"] == (
        f"SSH session '{session_id}' is no longer open: "
        "Session dont founded by key. Create session first!"
    )
    assert (await api.get("/api/ssh/status")).json()["connected"] is False


async def test_no_reply_from_worker_is_504(ssh, api, connect_body):
    await api.post("/api/ssh/connect", json=connect_body)
    response = await api.post(
        "/api/ssh/exec", json={"command": "sleep 1", "timeout": 0.1, "track_cwd": False}
    )
    assert response.status_code == 504
    assert response.json()["error"] == "ssh_exec_timeout"
    assert "may still be running" in response.json()["detail"]
    assert len(ssh.manager) == 1  # сессия в воркере жива


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


async def test_shared_connection_runs_commands_one_at_a_time(
    shell_worker, missing_socket_path, connect_body
):
    """Без отдельных подключений воркер выполняет запросы всех сессий по очереди.
    Таймаут команды второй сессии не тикает, пока первая занимает воркер."""
    client, worker = await make_api(shell_worker.port, missing_socket_path, channels=False)
    try:
        async with client:
            first = (await client.post("/api/ssh/connect", json=connect_body)).json()
            second = (await client.post("/api/ssh/connect", json=connect_body)).json()
            slow = client.post(
                "/api/ssh/exec",
                json={"command": "sleep 1; echo slow", "session_id": first["session_id"]},
            )
            fast = client.post(
                "/api/ssh/exec",
                json={
                    "command": "echo fast",
                    "timeout": 0.5,
                    "track_cwd": False,
                    "session_id": second["session_id"],
                },
            )
            slow_response, fast_response = await asyncio.gather(slow, fast)
            assert slow_response.json()["stdout"] == "slow"
            assert fast_response.status_code == 200, fast_response.text
            assert fast_response.json()["stdout"] == "fast"
    finally:
        await worker.disconnect()


async def test_cwd_persists_between_commands(shell_worker, missing_socket_path, connect_body):
    client, worker = await make_api(shell_worker.port, missing_socket_path)
    home = str(Path(shell_worker.home).resolve())
    async with client:
        connected = await client.post("/api/ssh/connect", json=connect_body)
        session_id = connected.json()["session_id"]

        first = (await client.post("/api/ssh/exec", json={"command": "pwd"})).json()
        assert first["duration_ms"] >= 0
        first.pop("duration_ms")
        assert first.pop("seq") == 3  # opened, command, result
        assert first == {
            "session_id": session_id,
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


async def test_cwd_is_kept_per_session(shell_worker, missing_socket_path, connect_body):
    client, worker = await make_api(shell_worker.port, missing_socket_path)
    async with client:
        first = (await client.post("/api/ssh/connect", json=connect_body)).json()["session_id"]
        second = (await client.post("/api/ssh/connect", json=connect_body)).json()["session_id"]

        async def run(command: str, session_id: str) -> dict:
            body = {"command": command, "session_id": session_id}
            return (await client.post("/api/ssh/exec", json=body)).json()

        assert (await run("cd /tmp", first))["cwd"] == "/tmp"
        assert (await run("cd /", second))["cwd"] == "/"
        assert (await run("pwd", first))["stdout"] == "/tmp"
        assert (await run("pwd", second))["stdout"] == "/"
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


# ---------------------------------------------------------- stored keys


async def test_connect_with_stored_key(api, fake_worker):
    keystore = get_keystore()
    record = await keystore.generate_key("prod")
    secret = await keystore.decrypt_private_key(record.id)

    body = {"host": "10.0.0.1", "username": "root", "key_id": record.id}
    response = await api.post("/api/ssh/connect", json=body)
    status = (await api.get("/api/ssh/status")).json()

    assert response.status_code == 201, response.text
    assert response.json()["key_id"] == record.id and status["key_id"] == record.id
    assert response.json()["auth"] == status["auth"] == "key_id"
    # Воркер получил путь к файлу с расшифрованным ключом...
    assert fake_worker.connect_key_files == [secret]
    sent_path = Path(fake_worker.requests[-1]["payload"]["connection"]["key_path"])
    assert sent_path.parent == keystore.runtime_dir
    # ...и файл удалён сразу после подключения
    assert not sent_path.exists()
    assert list(keystore.runtime_dir.iterdir()) == []


async def test_connect_with_stored_key_failure_cleans_up(api):
    keystore = get_keystore()
    record = await keystore.generate_key("prod")
    body = {"host": "unreachable", "username": "root", "key_id": record.id}
    response = await api.post("/api/ssh/connect", json=body)
    missing = await api.post(
        "/api/ssh/connect", json={"host": "h", "username": "u", "key_id": "0" * 32}
    )

    assert response.status_code == 502
    assert list(keystore.runtime_dir.iterdir()) == []
    assert missing.status_code == 404 and missing.json()["error"] == "key_not_found"
