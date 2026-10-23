from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi import FastAPI

from app.api.router import build_api_router
from app.core.errors import register_error_handlers
from app.plugins.manager import PluginManager

# Абсолютный на любой ОС путь к несуществующему файлу (на Windows "/x/y" не абсолютный)
MISSING_FILE = str(Path(__file__).resolve().with_name("definitely-missing") / "id_ed25519")


def openssh(key, password: bytes | None = None) -> str:
    encryption = ser.BestAvailableEncryption(password) if password else ser.NoEncryption()
    return key.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.OpenSSH, encryption).decode()


def public_of(key) -> str:
    return key.public_key().public_bytes(ser.Encoding.OpenSSH, ser.PublicFormat.OpenSSH).decode()


@pytest.fixture
async def api():
    app = FastAPI()
    register_error_handlers(app)
    api_router = build_api_router()
    PluginManager().discover(api_router, enabled=["keys"])
    app.include_router(api_router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_crud_flow(api, isolated_keystore):
    assert (await api.get("/api/keys")).json() == []

    key = ed25519.Ed25519PrivateKey.generate()
    created = await api.post(
        "/api/keys", json={"name": "prod", "private_key": openssh(key), "comment": "me@pc"}
    )
    assert created.status_code == 201, created.text
    info = created.json()
    assert set(info) == {
        "id", "name", "type", "bits", "fingerprint", "public_key", "comment",
        "created_at", "updated_at",
    }  # fmt: skip
    assert info["public_key"] == f"{public_of(key)} me@pc"
    assert (info["type"], info["bits"]) == ("ssh-ed25519", 256)

    key_id = info["id"]
    assert (await api.get(f"/api/keys/{key_id}")).json() == info
    assert [k["id"] for k in (await api.get("/api/keys")).json()] == [key_id]

    patched = await api.patch(f"/api/keys/{key_id}", json={"name": "production"})
    assert patched.status_code == 200
    assert patched.json()["name"] == "production"
    assert patched.json()["comment"] == "me@pc"

    deleted = await api.delete(f"/api/keys/{key_id}")
    assert deleted.status_code == 204 and deleted.content == b""
    missing = await api.get(f"/api/keys/{key_id}")
    assert missing.status_code == 404
    assert missing.json() == {"error": "key_not_found", "detail": f"SSH key '{key_id}' not found"}

    # Файл хранилища существует, но приватного ключа в нём нет
    assert "PRIVATE KEY" not in isolated_keystore.read_text()


async def test_generate(api):
    response = await api.post("/api/keys/generate", json={"name": "new", "comment": "synex"})
    assert response.status_code == 201
    info = response.json()
    assert info["type"] == "ssh-ed25519" and info["public_key"].endswith(" synex")

    rsa = await api.post("/api/keys/generate", json={"name": "rsa", "type": "rsa", "bits": 2048})
    assert (rsa.json()["type"], rsa.json()["bits"]) == ("ssh-rsa", 2048)

    bad = await api.post("/api/keys/generate", json={"name": "x", "type": "ed25519", "bits": 4096})
    assert bad.status_code == 422


async def test_import_from_path(api, tmp_path: Path):
    key = ed25519.Ed25519PrivateKey.generate()
    path = tmp_path / "id_ed25519"
    path.write_text(openssh(key, b"secret"))

    no_phrase = await api.post("/api/keys", json={"name": "k", "private_key_path": str(path)})
    assert no_phrase.status_code == 422
    assert no_phrase.json()["error"] == "invalid_key"
    assert "passphrase" in no_phrase.json()["detail"]

    response = await api.post(
        "/api/keys",
        json={"name": "k", "private_key_path": str(path), "passphrase": "secret"},
    )
    assert response.status_code == 201
    assert response.json()["public_key"] == public_of(key)


async def test_conflicts(api):
    key = openssh(ed25519.Ed25519PrivateKey.generate())
    assert (await api.post("/api/keys", json={"name": "a", "private_key": key})).status_code == 201

    same_key = await api.post("/api/keys", json={"name": "b", "private_key": key})
    assert same_key.status_code == 409
    assert same_key.json()["error"] == "key_exists"

    same_name = await api.post("/api/keys/generate", json={"name": "A"})
    assert same_name.status_code == 409

    other = (await api.post("/api/keys/generate", json={"name": "b"})).json()
    rename = await api.patch(f"/api/keys/{other['id']}", json={"name": "a"})
    assert rename.status_code == 409


@pytest.mark.parametrize(
    "body",
    [
        {"name": "x"},  # нет ключа
        {"name": "x", "private_key": "k", "private_key_path": "/k"},  # оба источника
        {"name": "  ", "private_key": "k"},
        {"name": "bad\nname", "private_key": "k"},
        {"name": "x", "private_key_path": "relative/key"},
        {"name": "x", "private_key_path": MISSING_FILE},
        {"name": "x" * 65, "private_key": "k"},
    ],
)
async def test_import_validation(api, body):
    response = await api.post("/api/keys", json=body)
    assert response.status_code == 422


async def test_import_path_errors_are_specific(api):
    relative = await api.post("/api/keys", json={"name": "x", "private_key_path": "rel/key"})
    assert "must be an absolute path" in relative.text
    missing = await api.post("/api/keys", json={"name": "x", "private_key_path": MISSING_FILE})
    assert "file not found" in missing.text


async def test_invalid_key_content(api):
    response = await api.post("/api/keys", json={"name": "x", "private_key": "not a key"})
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_key"


async def test_patch_validation(api):
    key_id = (await api.post("/api/keys/generate", json={"name": "k"})).json()["id"]
    assert (await api.patch(f"/api/keys/{key_id}", json={})).status_code == 422
    assert (await api.patch("/api/keys/missing", json={"name": "z"})).status_code == 404


async def test_private_key_never_leaves_the_api(api):
    key = ed25519.Ed25519PrivateKey.generate()
    pem = openssh(key)
    created = (await api.post("/api/keys", json={"name": "k", "private_key": pem})).json()
    body_line = pem.splitlines()[1]
    for response in (
        await api.get("/api/keys"),
        await api.get(f"/api/keys/{created['id']}"),
        await api.patch(f"/api/keys/{created['id']}", json={"comment": "c"}),
    ):
        assert "PRIVATE KEY" not in response.text
        assert body_line not in response.text
