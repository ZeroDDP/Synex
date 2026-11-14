"""Проверка ключа сервера (TOFU): отпечатки по host:port, подтверждение пользователем,
замена только по явному действию. Фейковый воркер ведёт себя как сборка 2026-11-14:
без host_key_fingerprint отвечает host_key_unknown, с чужим отпечатком host_key_mismatch.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.core.config import settings
from app.plugins.ssh.known_hosts import KnownHostsStore, normalize_host, server_name
from tests.fake_worker import FAKE_KEY_TYPE, fake_host_fingerprint
from tests.ssh_helpers import make_api

HOST = "10.0.0.1"
OTHER = "SHA256:" + "A" * 43


@pytest.fixture
def connect_body(tmp_path: Path) -> dict:
    key = tmp_path / "id_ed25519"
    key.write_text("key")
    return {"host": HOST, "username": "root", "key_path": str(key)}


@pytest.fixture
async def ssh(fake_worker, missing_socket_path):
    fake_worker.host_key_mode = "required"
    client, harness = await make_api(fake_worker.port, missing_socket_path)
    async with client:
        harness.api = client
        yield harness
    await harness.disconnect()


def connects(fake_worker) -> list[dict]:
    return [
        r["payload"]["connection"] for r in fake_worker.requests if r["action"] == "ssh_connect"
    ]


async def test_first_connection_asks_to_confirm_the_key(ssh, fake_worker, connect_body):
    real = fake_host_fingerprint(HOST, 22)

    unknown = await ssh.api.post("/api/ssh/sessions", json=connect_body)
    assert unknown.status_code == 409
    body = unknown.json()
    assert body["error"] == "ssh_host_key_unknown"
    assert body["host_key"] == {
        "host": HOST,
        "port": 22,
        "fingerprint": real,
        "key_type": FAKE_KEY_TYPE,
    }
    assert real in body["detail"] and "known-hosts" in body["detail"]
    assert (await ssh.api.get("/api/ssh/sessions")).json() == []
    # Сам backend ключ не сохраняет и подключение не повторяет
    assert (await ssh.api.get("/api/ssh/known-hosts")).json() == []
    assert len(connects(fake_worker)) == 1
    assert "host_key_fingerprint" not in connects(fake_worker)[0]

    # Пользователь подтвердил отпечаток: UI сохраняет его и подключается снова
    trusted = await ssh.api.post(
        "/api/ssh/known-hosts",
        json={"host": HOST, "port": 22, "fingerprint": real, "key_type": FAKE_KEY_TYPE},
    )
    assert trusted.status_code == 201, trusted.text
    assert trusted.json()["changed"] is True
    assert trusted.json()["known_host"]["fingerprint"] == real

    opened = await ssh.api.post("/api/ssh/sessions", json=connect_body)
    assert opened.status_code == 201, opened.text
    assert opened.json()["host_key"] == {"fingerprint": real, "key_type": FAKE_KEY_TYPE}
    assert connects(fake_worker)[-1]["host_key_fingerprint"] == real

    # Ключ сохраняется по host:port: другой порт того же хоста снова спросит
    other_port = await ssh.api.post("/api/ssh/sessions", json={**connect_body, "port": 2222})
    assert (other_port.status_code, other_port.json()["error"]) == (409, "ssh_host_key_unknown")
    assert other_port.json()["host_key"]["fingerprint"] == fake_host_fingerprint(HOST, 2222)


async def test_changed_key_is_not_retried_and_not_replaced_silently(ssh, fake_worker, connect_body):
    real = fake_host_fingerprint(HOST, 22)
    await ssh.api.post("/api/ssh/known-hosts", json={"host": HOST, "fingerprint": real})
    assert (await ssh.api.post("/api/ssh/sessions", json=connect_body)).status_code == 201

    # Ключ сервера сменился (переустановка или подмена)
    new_key = fake_host_fingerprint("reinstalled", 22)
    fake_worker.host_keys[(HOST, 22)] = new_key
    before = len(connects(fake_worker))
    changed = await ssh.api.post("/api/ssh/sessions", json=connect_body)
    assert changed.status_code == 409
    body = changed.json()
    assert body["error"] == "ssh_host_key_mismatch"
    assert body["host_key"] == {
        "host": HOST,
        "port": 22,
        "fingerprint": new_key,
        "key_type": FAKE_KEY_TYPE,
        "expected_fingerprint": real,
    }
    assert "man in the middle" in body["detail"]
    assert len(connects(fake_worker)) == before + 1  # одна попытка, без повтора
    # Сохранённый отпечаток прежний
    [known] = (await ssh.api.get("/api/ssh/known-hosts")).json()
    assert known["fingerprint"] == real

    # Без явного replace новый ключ не сохранить
    conflict = await ssh.api.post(
        "/api/ssh/known-hosts", json={"host": HOST, "fingerprint": new_key}
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "ssh_known_host_conflict"
    assert conflict.json()["host_key"]["expected_fingerprint"] == real
    assert (await ssh.api.get("/api/ssh/known-hosts")).json()[0]["fingerprint"] == real

    # Пользователь явно подтвердил новый ключ
    replaced = await ssh.api.post(
        "/api/ssh/known-hosts",
        json={"host": HOST, "fingerprint": new_key, "key_type": FAKE_KEY_TYPE, "replace": True},
    )
    assert replaced.status_code == 201 and replaced.json()["changed"] is True
    assert (await ssh.api.post("/api/ssh/sessions", json=connect_body)).status_code == 201


async def test_forget_a_key(ssh, fake_worker, connect_body):
    real = fake_host_fingerprint(HOST, 22)
    await ssh.api.post("/api/ssh/known-hosts", json={"host": HOST, "fingerprint": real})
    # Тот же отпечаток ещё раз: ничего не меняется
    again = await ssh.api.post("/api/ssh/known-hosts", json={"host": HOST, "fingerprint": real})
    assert again.status_code == 201 and again.json()["changed"] is False

    forgotten = await ssh.api.delete("/api/ssh/known-hosts", params={"host": HOST})
    assert forgotten.status_code == 200 and forgotten.json()["fingerprint"] == real
    missing = await ssh.api.delete("/api/ssh/known-hosts", params={"host": HOST})
    assert (missing.status_code, missing.json()["error"]) == (404, "ssh_known_host_not_found")
    unknown = await ssh.api.post("/api/ssh/sessions", json=connect_body)
    assert unknown.json()["error"] == "ssh_host_key_unknown"


async def test_bad_fingerprint_is_rejected(ssh):
    for bad in ("", "MD5:aa:bb", "SHA256:short", "SHA256:" + "A" * 43 + "="):
        response = await ssh.api.post(
            "/api/ssh/known-hosts", json={"host": HOST, "fingerprint": bad}
        )
        assert response.status_code == 422, bad
    assert (await ssh.api.get("/api/ssh/known-hosts")).json() == []


async def test_companion_uses_the_key_of_its_session(ssh, fake_worker, connect_body):
    real = fake_host_fingerprint(HOST, 22)
    await ssh.api.post("/api/ssh/known-hosts", json={"host": HOST, "fingerprint": real})
    session_id = (await ssh.api.post("/api/ssh/sessions", json=connect_body)).json()["session_id"]
    companion = await ssh.manager.open_companion(session_id, "test")
    assert companion is not None
    assert connects(fake_worker)[-1]["host_key_fingerprint"] == real

    # Ключ сервера сменился, пока сессия открыта: служебная сессия не подключается
    fake_worker.host_keys[(HOST, 22)] = OTHER
    with pytest.raises(Exception) as error:
        await ssh.manager.open_companion(session_id, "test")
    assert getattr(error.value, "code", None) == "ssh_host_key_mismatch"


async def test_worker_without_tofu_keeps_working(fake_worker, missing_socket_path, connect_body):
    """Сборки воркера до 2026-11-14 ключ не проверяют и отпечаток не сообщают: backend
    подключается как раньше (и с сохранённым отпечатком), у сессии host_key null."""
    fake_worker.host_key_mode = "none"
    client, harness = await make_api(fake_worker.port, missing_socket_path)
    async with client:
        response = await client.post("/api/ssh/sessions", json=connect_body)
        assert response.status_code == 201, response.text
        assert response.json()["host_key"] is None
        await client.post("/api/ssh/known-hosts", json={"host": HOST, "fingerprint": OTHER})
        again = await client.post("/api/ssh/sessions", json=connect_body)
        assert again.status_code == 201, again.text
    await harness.disconnect()


# ------------------------------------------------------------------ store


async def test_store_file(tmp_path: Path):
    path = tmp_path / "data" / "known_hosts.json"
    store = KnownHostsStore(path)
    real = fake_host_fingerprint(HOST, 22)
    record, changed = await store.trust(" Example.COM ", 22, real, "ssh-ed25519")
    assert changed and record.host == "example.com"
    assert await store.fingerprint("example.com", 22) == real
    assert await store.fingerprint("EXAMPLE.com", 22) == real  # регистр и пробелы не важны
    assert await store.fingerprint("example.com", 2222) is None
    await store.trust("[::1]", 22, real)
    assert await store.fingerprint("::1", 22) == real

    content = json.loads(path.read_text(encoding="utf-8"))
    assert content["version"] == 1
    assert [(h["host"], h["port"]) for h in content["hosts"]] == [("example.com", 22), ("::1", 22)]
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


async def test_damaged_file(tmp_path: Path):
    path = tmp_path / "known_hosts.json"
    path.write_text("{not json", encoding="utf-8")
    store = KnownHostsStore(path)
    with pytest.raises(Exception) as error:
        await store.list_hosts()
    assert getattr(error.value, "code", None) == "known_hosts_corrupted"


def test_path_follows_settings(monkeypatch, tmp_path: Path):
    from app.plugins.ssh.known_hosts import get_known_hosts

    monkeypatch.setattr(settings, "KNOWN_HOSTS_PATH", tmp_path / "a.json")
    assert get_known_hosts().path == tmp_path / "a.json"
    monkeypatch.setattr(settings, "KNOWN_HOSTS_PATH", tmp_path / "b.json")
    assert get_known_hosts().path == tmp_path / "b.json"


def test_names():
    assert normalize_host(" [FE80::1] ") == "fe80::1"
    assert server_name("FE80::1", 22) == "[fe80::1]:22"
    assert server_name("Host", 2222) == "host:2222"
