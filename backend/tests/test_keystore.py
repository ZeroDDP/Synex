from __future__ import annotations

import base64
import hashlib
import json
import stat
import sys
from pathlib import Path

import keyring
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed25519, rsa
from keyring.backend import KeyringBackend
from keyring.errors import NoKeyringError, PasswordDeleteError
from pydantic import SecretStr

from app.core.config import settings
from app.keystore import (
    InvalidKeyError,
    KeyExistsError,
    KeyNotFoundError,
    KeyStore,
    KeystoreCorruptedError,
    KeystoreUnavailableError,
    get_keystore,
)
from app.keystore.master_key import KEYRING_SERVICE, KEYRING_USERNAME
from app.keystore.ssh_keys import parse_private_key

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")


def openssh(key, password: bytes | None = None) -> bytes:
    encryption = ser.BestAvailableEncryption(password) if password else ser.NoEncryption()
    return key.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.OpenSSH, encryption)


def public_of(key) -> str:
    return key.public_key().public_bytes(ser.Encoding.OpenSSH, ser.PublicFormat.OpenSSH).decode()


@pytest.fixture
def store(isolated_keystore: Path) -> KeyStore:
    return KeyStore(isolated_keystore)


# ------------------------------------------------------------------ parsing


def test_fingerprint_matches_ssh_keygen_algorithm():
    key = ed25519.Ed25519PrivateKey.generate()
    parsed = parse_private_key(openssh(key))
    blob = base64.b64decode(public_of(key).split()[1])
    expected = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    assert parsed.fingerprint == expected
    assert (parsed.key_type, parsed.bits, parsed.public_key) == ("ssh-ed25519", 256, public_of(key))


@pytest.mark.parametrize(
    ("key", "fmt", "key_type", "bits"),
    [
        (ed25519.Ed25519PrivateKey.generate(), "openssh", "ssh-ed25519", 256),
        (rsa.generate_private_key(65537, 2048), "pkcs1", "ssh-rsa", 2048),
        (rsa.generate_private_key(65537, 3072), "pkcs8", "ssh-rsa", 3072),
        (ec.generate_private_key(ec.SECP256R1()), "openssh", "ecdsa-sha2-nistp256", 256),
        (ec.generate_private_key(ec.SECP384R1()), "pkcs8", "ecdsa-sha2-nistp384", 384),
    ],
)
def test_supported_formats(key, fmt, key_type, bits):
    if fmt == "openssh":
        data = openssh(key)
    else:
        private_format = (
            ser.PrivateFormat.TraditionalOpenSSL if fmt == "pkcs1" else ser.PrivateFormat.PKCS8
        )
        data = key.private_bytes(ser.Encoding.PEM, private_format, ser.NoEncryption())
    # Ключ, вставленный из буфера Windows, приходит с \r\n
    parsed = parse_private_key(data.replace(b"\n", b"\r\n"))
    assert (parsed.key_type, parsed.bits) == (key_type, bits)
    # Хранится всегда в OpenSSH-формате, который читает russh
    assert parsed.private_key.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----")
    assert public_of(ser.load_ssh_private_key(parsed.private_key, None)) == public_of(key)


def test_passphrase_handling():
    key = ed25519.Ed25519PrivateKey.generate()
    protected = openssh(key, b"secret")

    with pytest.raises(InvalidKeyError, match="passphrase"):
        parse_private_key(protected)
    with pytest.raises(InvalidKeyError, match="Wrong passphrase"):
        parse_private_key(protected, b"wrong")

    parsed = parse_private_key(protected, b"secret")
    # Сохраняется без парольной фразы: воркер не умеет их вводить
    assert public_of(ser.load_ssh_private_key(parsed.private_key, None)) == public_of(key)

    # Лишняя парольная фраза к ключу без неё не мешает
    assert parse_private_key(openssh(key), b"unneeded").fingerprint == parsed.fingerprint

    pkcs8 = rsa.generate_private_key(65537, 2048).private_bytes(
        ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.BestAvailableEncryption(b"pw")
    )
    with pytest.raises(InvalidKeyError, match="Wrong passphrase"):
        parse_private_key(pkcs8, b"bad")
    assert parse_private_key(pkcs8, b"pw").key_type == "ssh-rsa"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"garbage", "Not a private key"),
        (b"PuTTY-User-Key-File-3: ssh-ed25519\n", "PuTTY"),
        (b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMq user@host", "Not a private key"),
        (b"-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----", "Cannot"),
        (
            rsa.generate_private_key(65537, 1024).private_bytes(
                ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption()
            ),
            "shorter than 2048",
        ),
        (
            dsa.generate_private_key(2048).private_bytes(
                ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption()
            ),
            "Unsupported key type",
        ),
        (
            ec.generate_private_key(ec.SECP256K1()).private_bytes(
                ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption()
            ),
            "Unsupported ECDSA curve",
        ),
    ],
)
def test_rejected_keys(data, message):
    with pytest.raises(InvalidKeyError, match=message):
        parse_private_key(data)


# -------------------------------------------------------------------- store


async def test_import_and_decrypt(store: KeyStore):
    key = ed25519.Ed25519PrivateKey.generate()
    record = await store.import_key("prod", openssh(key), comment="me@laptop")

    assert len(record.id) == 32
    assert record.public_key_line == f"{public_of(key)} me@laptop"
    assert await store.get_key(record.id) == record
    assert [r.id for r in await store.list_keys()] == [record.id]

    decrypted = await store.decrypt_private_key(record.id)
    assert public_of(ser.load_ssh_private_key(decrypted, None)) == public_of(key)


async def test_file_never_contains_plaintext_private_key(store: KeyStore, isolated_keystore):
    key = ed25519.Ed25519PrivateKey.generate()
    plain = openssh(key)
    await store.import_key("prod", plain)
    await store.generate_key("generated")

    raw = isolated_keystore.read_text()
    assert "PRIVATE KEY" not in raw
    body = b"".join(plain.splitlines()[1:-1]).decode()
    assert body[:40] not in raw
    content = json.loads(raw)
    assert content["version"] == 1
    assert all(k["private_key"].startswith("gAAAA") for k in content["keys"])  # Fernet-токены
    assert not list(isolated_keystore.parent.glob("*.tmp"))


@posix_only
async def test_file_permissions(store: KeyStore, isolated_keystore):
    await store.generate_key("k")
    assert stat.S_IMODE(isolated_keystore.stat().st_mode) == 0o600
    assert stat.S_IMODE(isolated_keystore.parent.stat().st_mode) == 0o700


async def test_generate_rsa(store: KeyStore):
    record = await store.generate_key("rsa", "rsa", 2048, comment="c")
    assert (record.key_type, record.bits) == ("ssh-rsa", 2048)
    decrypted = await store.decrypt_private_key(record.id)
    assert ser.load_ssh_private_key(decrypted, None).key_size == 2048


async def test_duplicates_are_rejected(store: KeyStore):
    key = openssh(ed25519.Ed25519PrivateKey.generate())
    await store.import_key("Prod", key)
    with pytest.raises(KeyExistsError, match="already exists"):
        await store.generate_key("prod")  # имена сравниваются без учёта регистра
    with pytest.raises(KeyExistsError, match="already stored as 'Prod'"):
        await store.import_key("other", key)


async def test_update_and_delete(store: KeyStore):
    first = await store.generate_key("first")
    second = await store.generate_key("second")

    updated = await store.update_key(first.id, name="renamed", comment="note")
    assert (updated.name, updated.comment) == ("renamed", "note")
    assert updated.updated_at > first.updated_at
    with pytest.raises(KeyExistsError):
        await store.update_key(first.id, name="SECOND")
    assert (await store.update_key(first.id, name="Renamed")).name == "Renamed"

    await store.delete_key(first.id)
    assert [r.id for r in await store.list_keys()] == [second.id]
    for call in (store.get_key, store.delete_key, store.decrypt_private_key):
        with pytest.raises(KeyNotFoundError):
            await call(first.id)


async def test_empty_store_reads_without_master_key(store: KeyStore, monkeypatch):
    monkeypatch.setattr(settings, "KEYSTORE_MASTER_KEY", SecretStr("not-a-fernet-key"))
    assert await store.list_keys() == []
    with pytest.raises(KeyNotFoundError):
        await store.get_key("missing")


async def test_reopen_with_same_and_other_master_key(
    store: KeyStore, isolated_keystore, monkeypatch
):
    record = await store.generate_key("k")

    reopened = KeyStore(isolated_keystore)
    assert (await reopened.decrypt_private_key(record.id)).startswith(b"-----BEGIN OPENSSH")

    monkeypatch.setattr(settings, "KEYSTORE_MASTER_KEY", SecretStr(Fernet.generate_key().decode()))
    stranger = KeyStore(isolated_keystore)
    assert [r.id for r in await stranger.list_keys()] == [record.id]  # метаданные читаются
    with pytest.raises(KeystoreCorruptedError, match="does not match"):
        await stranger.decrypt_private_key(record.id)
    with pytest.raises(KeystoreCorruptedError, match="does not match"):
        await stranger.generate_key("other")


async def test_invalid_master_key(store: KeyStore, monkeypatch):
    monkeypatch.setattr(settings, "KEYSTORE_MASTER_KEY", SecretStr("not-a-fernet-key"))
    with pytest.raises(KeystoreUnavailableError, match="not a valid Fernet key"):
        await store.generate_key("k")


async def test_corrupted_file_is_not_overwritten(store: KeyStore, isolated_keystore):
    isolated_keystore.parent.mkdir(parents=True)
    isolated_keystore.write_text("{not json")
    with pytest.raises(KeystoreCorruptedError, match="damaged"):
        await store.list_keys()
    with pytest.raises(KeystoreCorruptedError):
        await store.generate_key("k")
    assert isolated_keystore.read_text() == "{not json"


async def test_temporary_key_file(store: KeyStore):
    record = await store.generate_key("k")
    secret = await store.decrypt_private_key(record.id)

    async with store.temporary_key_file(record.id) as path:
        assert path.parent == store.runtime_dir
        assert path.read_bytes() == secret
        if sys.platform != "win32":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not path.exists()

    with pytest.raises(RuntimeError):
        async with store.temporary_key_file(record.id) as path:
            raise RuntimeError("connect failed")
    assert not path.exists()

    with pytest.raises(KeyNotFoundError):
        async with store.temporary_key_file("missing"):
            pass

    leftover = store.runtime_dir / "key_leftover"
    leftover.write_bytes(b"secret")
    store.cleanup_runtime()
    assert not leftover.exists()


def test_get_keystore_follows_settings(isolated_keystore, monkeypatch, tmp_path):
    assert get_keystore().path == isolated_keystore
    assert get_keystore() is get_keystore()
    monkeypatch.setattr(settings, "KEYSTORE_PATH", tmp_path / "other.json")
    assert get_keystore().path == tmp_path / "other.json"


# ---------------------------------------------------------------- OS keychain


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.passwords: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.passwords.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.passwords[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if self.passwords.pop((service, username), None) is None:
            raise PasswordDeleteError(username)


class BrokenKeyring(MemoryKeyring):
    def get_password(self, service: str, username: str) -> str | None:
        raise NoKeyringError("No recommended backend was available")


@pytest.fixture
def memory_keyring(monkeypatch):
    backend = MemoryKeyring()
    previous = keyring.get_keyring()
    keyring.set_keyring(backend)
    monkeypatch.setattr(settings, "KEYSTORE_MASTER_KEY", None)
    yield backend
    keyring.set_keyring(previous)


async def test_master_key_is_created_in_os_keychain(memory_keyring, isolated_keystore):
    record = await KeyStore(isolated_keystore).generate_key("k")
    stored = memory_keyring.passwords[(KEYRING_SERVICE, KEYRING_USERNAME)]
    Fernet(stored.encode())  # валидный ключ
    assert stored not in isolated_keystore.read_text()

    # Новый экземпляр (перезапуск приложения) использует тот же ключ
    assert await KeyStore(isolated_keystore).decrypt_private_key(record.id)


async def test_missing_master_key_is_not_silently_replaced(memory_keyring, isolated_keystore):
    record = await KeyStore(isolated_keystore).generate_key("k")
    memory_keyring.passwords.clear()  # ключ удалили из Credential Manager

    with pytest.raises(KeystoreCorruptedError, match="missing from the OS keychain"):
        await KeyStore(isolated_keystore).decrypt_private_key(record.id)
    assert memory_keyring.passwords == {}  # новый ключ не создан


async def test_keychain_unavailable(isolated_keystore, monkeypatch):
    previous = keyring.get_keyring()
    keyring.set_keyring(BrokenKeyring())
    monkeypatch.setattr(settings, "KEYSTORE_MASTER_KEY", None)
    try:
        with pytest.raises(KeystoreUnavailableError, match="KEYSTORE_MASTER_KEY"):
            await KeyStore(isolated_keystore).generate_key("k")
    finally:
        keyring.set_keyring(previous)
