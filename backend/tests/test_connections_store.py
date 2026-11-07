"""Хранилище сохранённых подключений (connections.json)."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from app.core.config import settings
from app.keystore import KeyStore
from app.plugins.connections.errors import (
    ConnectionExistsError,
    ConnectionNotFoundError,
    ConnectionsCorruptedError,
    InvalidConnectionError,
)
from app.plugins.connections.store import (
    KEEP,
    ConnectionAuth,
    ConnectionStore,
    get_connection_store,
)

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")


@pytest.fixture
def path(isolated_keystore: Path) -> Path:
    return isolated_keystore.with_name("connections.json")


@pytest.fixture
def store(path: Path, isolated_keystore: Path) -> ConnectionStore:
    return ConnectionStore(path, KeyStore(isolated_keystore))


async def test_create_list_and_secrets(store: ConnectionStore, path: Path):
    assert await store.list_connections() == []
    by_password = await store.create(
        name="prod",
        host="10.0.0.1",
        username="root",
        group="Work",
        auth=ConnectionAuth("password", password="s3cret!"),
    )
    by_key = await store.create(
        name="dev",
        host="dev.local",
        port=2222,
        username="deploy",
        auth=ConnectionAuth("key_id", key_id="abc"),
    )
    ask = await store.create(
        name="ask", host="h", username="u", auth=ConnectionAuth("password")
    )  # пароль спрашивать при подключении

    assert [c.name for c in await store.list_connections()] == ["prod", "dev", "ask"]
    assert (by_key.port, by_key.auth, by_key.key_id, by_key.password) == (
        2222,
        "key_id",
        "abc",
        None,
    )
    assert await store.secrets(by_password) == ("s3cret!", None)
    assert await store.secrets(ask) == (None, None)
    assert (await store.get(by_password.id)).group == "Work"

    text = path.read_text(encoding="utf-8")
    assert "s3cret!" not in text  # пароль только зашифрованным
    assert json.loads(text)["connections"][0]["password"] == by_password.password


async def test_key_path_with_passphrase(store: ConnectionStore):
    record = await store.create(
        name="k",
        host="h",
        username="u",
        auth=ConnectionAuth("key_path", key_path="/keys/id", passphrase="phrase"),
    )
    assert await store.secrets(record) == (None, "phrase")


@pytest.mark.parametrize(
    ("auth", "message"),
    [
        (ConnectionAuth("key_id"), "key_id is required"),
        (ConnectionAuth("key_id", key_id="a", password="p"), "takes only key_id"),
        (ConnectionAuth("key_path"), "key_path is required"),
        (ConnectionAuth("key_path", key_path="/k", password="p"), "optional passphrase"),
        (ConnectionAuth("password", key_id="a"), "only an optional password"),
        (ConnectionAuth("password", passphrase="x"), "only an optional password"),
    ],
)
async def test_inconsistent_auth_is_rejected(store: ConnectionStore, auth, message):
    with pytest.raises(InvalidConnectionError, match=message):
        await store.create(name="x", host="h", username="u", auth=auth)


async def test_names_are_unique(store: ConnectionStore):
    auth = ConnectionAuth("password")
    first = await store.create(name="Prod", host="h", username="u", auth=auth)
    with pytest.raises(ConnectionExistsError):
        await store.create(name="prod", host="h2", username="u", auth=auth)
    second = await store.create(name="dev", host="h", username="u", auth=auth)
    with pytest.raises(ConnectionExistsError):
        await store.update(second.id, name="PROD")
    renamed = await store.update(first.id, name="prod")  # тот же, другой регистр
    assert renamed.name == "prod"


async def test_update_keeps_or_replaces_secrets(store: ConnectionStore):
    record = await store.create(
        name="p", host="h", username="u", auth=ConnectionAuth("password", password="old")
    )
    moved = await store.update(record.id, host="new-host", port=2200)
    assert (moved.host, moved.port) == ("new-host", 2200)
    assert moved.updated_at >= record.updated_at
    assert await store.secrets(moved) == ("old", None)

    kept = await store.update(record.id, auth=ConnectionAuth("password", password=KEEP))
    assert await store.secrets(kept) == ("old", None)
    changed = await store.update(record.id, auth=ConnectionAuth("password", password="new"))
    assert await store.secrets(changed) == ("new", None)
    forgotten = await store.update(record.id, auth=ConnectionAuth("password"))
    assert forgotten.password is None

    await store.update(record.id, auth=ConnectionAuth("password", password="again"))
    # Сменили способ входа: KEEP не переносит старый пароль в новый способ
    switched = await store.update(
        record.id, auth=ConnectionAuth("key_path", key_path="/k", passphrase=KEEP)
    )
    assert (switched.auth, switched.password, switched.passphrase) == ("key_path", None, None)


async def test_delete_mark_connected_and_not_found(store: ConnectionStore):
    record = await store.create(name="p", host="h", username="u", auth=ConnectionAuth("password"))
    assert record.last_connected_at is None
    await store.mark_connected(record.id)
    assert (await store.get(record.id)).last_connected_at is not None

    await store.delete(record.id)
    assert await store.list_connections() == []
    for call in (store.get, store.delete, store.mark_connected):
        with pytest.raises(ConnectionNotFoundError):
            await call(record.id)
    with pytest.raises(ConnectionNotFoundError):
        await store.update(record.id, name="x")


async def test_corrupted_file_is_not_overwritten(store: ConnectionStore, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConnectionsCorruptedError, match="damaged"):
        await store.list_connections()
    with pytest.raises(ConnectionsCorruptedError):
        await store.create(name="p", host="h", username="u", auth=ConnectionAuth("password"))
    assert path.read_text(encoding="utf-8") == "{not json"


@posix_only
async def test_file_permissions(store: ConnectionStore, path: Path):
    await store.create(name="p", host="h", username="u", auth=ConnectionAuth("password"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_store_follows_settings(monkeypatch, tmp_path: Path):
    first = get_connection_store()
    assert first.path == settings.connections_path
    assert get_connection_store() is first
    monkeypatch.setattr(settings, "CONNECTIONS_PATH", tmp_path / "other.json")
    assert get_connection_store().path == tmp_path / "other.json"
