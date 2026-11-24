"""API сохранённых подключений /api/connections."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.api.router import build_api_router
from app.core.errors import register_error_handlers
from app.core.event_bus import event_bus
from app.plugins.manager import PluginManager
from app.plugins.ssh.manager import SSH_SESSION_EVENT, SSHSessionManager
from app.plugins.ssh.routes import get_ssh_manager
from tests.conftest import FakeWorkerProvider
from tests.fake_worker import PASSWORD

INFO_FIELDS = {
    "id", "name", "host", "port", "username", "group", "auth", "key_id", "key_path",
    "has_password", "has_passphrase", "created_at", "updated_at", "last_connected_at",
}  # fmt: skip


@pytest.fixture
async def api():
    app = FastAPI()
    register_error_handlers(app)
    api_router = build_api_router()
    PluginManager().discover(api_router, enabled=["connections", "keys"])
    app.include_router(api_router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def key_file(tmp_path: Path) -> str:
    path = tmp_path / "id_ed25519"
    path.write_text("key")
    return str(path)


async def create(api, **body) -> dict:
    response = await api.post("/api/connections", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def test_crud_flow(api, isolated_keystore):
    created = await create(
        api,
        name="prod",
        host=" 10.0.0.1 ",
        username="root",
        group="Work",
        auth="password",
        password="s3cret!",
    )
    assert set(created) == INFO_FIELDS
    assert (created["host"], created["port"], created["has_password"]) == ("10.0.0.1", 22, True)
    assert created["last_connected_at"] is None
    connection_id = created["id"]

    assert (await api.get(f"/api/connections/{connection_id}")).json() == created
    assert [c["id"] for c in (await api.get("/api/connections")).json()] == [connection_id]
    saved = isolated_keystore.with_name("connections.json").read_text(encoding="utf-8")
    assert "s3cret!" not in saved

    patched = await api.patch(f"/api/connections/{connection_id}", json={"port": 2222})
    assert patched.status_code == 200, patched.text
    assert (patched.json()["port"], patched.json()["has_password"]) == (2222, True)

    forgot = await api.patch(f"/api/connections/{connection_id}", json={"password": None})
    assert forgot.json()["has_password"] is False

    assert (await api.delete(f"/api/connections/{connection_id}")).status_code == 204
    missing = await api.get(f"/api/connections/{connection_id}")
    assert missing.status_code == 404
    assert missing.json()["error"] == "connection_not_found"


async def test_key_from_storage(api):
    key = (await api.post("/api/keys/generate", json={"name": "server"})).json()
    created = await create(api, name="k", host="h", username="u", auth="key_id", key_id=key["id"])
    assert (created["auth"], created["key_id"]) == ("key_id", key["id"])

    unknown = await api.post(
        "/api/connections",
        json={"name": "x", "host": "h", "username": "u", "auth": "key_id", "key_id": "nope"},
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"] == "key_not_found"


async def test_switch_auth_method(api, key_file):
    created = await create(api, name="p", host="h", username="u", auth="password", password="x")
    switched = await api.patch(
        f"/api/connections/{created['id']}",
        json={"auth": "key_path", "key_path": key_file, "passphrase": "phrase"},
    )
    assert switched.status_code == 200, switched.text
    body = switched.json()
    assert (body["auth"], body["key_path"]) == ("key_path", str(Path(key_file)))
    assert (body["has_password"], body["has_passphrase"]) == (False, True)

    # Тот же способ: можно сменить только фразу, путь остаётся
    phrase = await api.patch(f"/api/connections/{created['id']}", json={"passphrase": None})
    assert (phrase.json()["key_path"], phrase.json()["has_passphrase"]) == (body["key_path"], False)


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"auth": "key_id"}, "invalid_connection"),  # нет key_id
        ({"auth": "password", "key_id": "abc"}, "invalid_connection"),
        ({"auth": "key_path", "key_path": "relative/key"}, None),  # 422 валидации
        ({"auth": "key_path", "key_path": "/definitely/missing/key"}, None),
        ({"auth": "telnet"}, None),
    ],
)
async def test_invalid_auth(api, body, error):
    response = await api.post(
        "/api/connections", json={"name": "x", "host": "h", "username": "u", **body}
    )
    assert response.status_code == 422, response.text
    if error:
        assert response.json()["error"] == error


async def test_update_rules(api):
    first = await create(api, name="a", host="h", username="u", auth="password")
    await create(api, name="b", host="h", username="u", auth="password")
    url = f"/api/connections/{first['id']}"

    taken = await api.patch(url, json={"name": "B"})
    assert taken.status_code == 409
    assert taken.json()["error"] == "connection_exists"
    assert (await api.patch(url, json={"host": None})).status_code == 422
    # Фраза не относится к входу по паролю
    wrong = await api.patch(url, json={"passphrase": "x"})
    assert wrong.status_code == 422
    assert wrong.json()["error"] == "invalid_connection"
    group = await api.patch(url, json={"group": "Home"})
    assert group.json()["group"] == "Home"
    assert (await api.patch(url, json={"group": None})).json()["group"] is None


async def test_duplicate_name(api):
    await create(api, name="prod", host="h", username="u", auth="password")
    response = await api.post(
        "/api/connections",
        json={"name": "PROD", "host": "h", "username": "u", "auth": "password"},
    )
    assert response.status_code == 409


# ------------------------------------------------------------- connect


@pytest.fixture
async def ssh_api(fake_worker, missing_socket_path):
    """/api/connections вместе с плагином ssh, подключённым к фейковому воркеру."""
    provider = FakeWorkerProvider(fake_worker.port, missing_socket_path)
    await provider.start()
    manager = SSHSessionManager(provider)
    app = FastAPI()
    register_error_handlers(app)
    api_router = build_api_router()
    PluginManager().discover(api_router, enabled=["connections", "keys"])  # + ssh
    app.include_router(api_router)
    app.dependency_overrides[get_ssh_manager] = lambda: manager
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    await manager.close_all(reason="test")
    await provider.close()


@pytest.fixture
def events():
    received: list[dict] = []

    async def collect(event: dict) -> None:
        received.append(event)

    event_bus.subscribe(SSH_SESSION_EVENT, collect)
    yield received
    event_bus.unsubscribe(SSH_SESSION_EVENT, collect)


async def test_connect_with_saved_password(ssh_api, fake_worker, events):
    saved = await create(
        ssh_api, name="prod", host="10.0.0.5", port=2222, username="deploy",
        auth="password", password=PASSWORD,
    )  # fmt: skip
    response = await ssh_api.post(f"/api/connections/{saved['id']}/connect")
    assert response.status_code == 201, response.text
    session = response.json()
    assert (session["host"], session["port"], session["username"]) == ("10.0.0.5", 2222, "deploy")
    assert (session["auth"], session["connection_id"]) == ("password", saved["id"])

    listed = (await ssh_api.get("/api/ssh/sessions")).json()
    assert [s["connection_id"] for s in listed] == [saved["id"]]
    assert events[0]["connection_id"] == saved["id"]
    assert (
        next(
            r["payload"]["connection"]
            for r in reversed(fake_worker.requests)
            if r["action"] == "ssh_connect"
        )["password"]
        == PASSWORD
    )

    after = (await ssh_api.get(f"/api/connections/{saved['id']}")).json()
    assert after["last_connected_at"] is not None


async def test_password_asked_at_connect(ssh_api):
    saved = await create(ssh_api, name="ask", host="h", username="u", auth="password")
    url = f"/api/connections/{saved['id']}/connect"

    missing = await ssh_api.post(url)
    assert missing.status_code == 422
    assert missing.json()["error"] == "connection_secret_required"

    wrong = await ssh_api.post(url, json={"password": "wrong"})
    assert wrong.status_code == 502
    assert wrong.json()["error"] == "ssh_connect_failed"
    after = (await ssh_api.get(f"/api/connections/{saved['id']}")).json()
    assert after["last_connected_at"] is None  # не подключились

    ok = await ssh_api.post(url, json={"password": PASSWORD})
    assert ok.status_code == 201, ok.text


async def test_connect_with_stored_key_and_key_file(ssh_api, fake_worker, key_file):
    key = (await ssh_api.post("/api/keys/generate", json={"name": "server"})).json()
    by_key = await create(
        ssh_api, name="k", host="h", username="u", auth="key_id", key_id=key["id"]
    )
    session = (await ssh_api.post(f"/api/connections/{by_key['id']}/connect")).json()
    assert (session["auth"], session["key_id"]) == ("key_id", key["id"])
    assert fake_worker.connect_key_files[-1].startswith(b"-----BEGIN OPENSSH PRIVATE KEY")

    by_file = await create(
        ssh_api, name="f", host="h", username="u", auth="key_path", key_path=key_file
    )
    # Пароль к входу по ключу не относится
    wrong = await ssh_api.post(f"/api/connections/{by_file['id']}/connect", json={"password": "x"})
    assert wrong.status_code == 422
    assert wrong.json()["error"] == "invalid_connection"
    assert (await ssh_api.post(f"/api/connections/{by_file['id']}/connect")).status_code == 201

    Path(key_file).unlink()  # файл ключа удалили после сохранения
    gone = await ssh_api.post(f"/api/connections/{by_file['id']}/connect")
    assert gone.status_code == 422
    assert "not found" in gone.json()["detail"]


async def test_connect_unknown_connection(ssh_api):
    response = await ssh_api.post("/api/connections/0123456789abcdef/connect")
    assert response.status_code == 404
    assert response.json()["error"] == "connection_not_found"
