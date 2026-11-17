"""Проверка ключа сервера (TOFU) с настоящим воркером: UI → FastAPI → synex_worker → sshd.

Воркер (с 2026-11-14) без отпечатка не подключается и сообщает ключ сервера; backend
отдаёт его UI в 409 ssh_host_key_unknown и после подтверждения пользователем
подключается с ним. Чужой отпечаток даёт 409 ssh_host_key_mismatch без повтора. Ключ,
который записан в known_hosts OpenSSH, подтверждать не нужно.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest

from app.core.config import settings
from tests.integration.sshd import SSHTarget
from tests.integration.ui import UI

WRONG = "SHA256:" + "A" * 43


@pytest.fixture(autouse=True)
def all_plugins(monkeypatch):
    monkeypatch.setattr(settings, "PLUGINS_ENABLED", "*")


async def test_server_key_is_confirmed_once_and_checked_every_time(ui: UI, ssh_target: SSHTarget):
    where = {"host": ssh_target.host, "port": ssh_target.port}
    # Фикстура ui уже подтвердила ключ: начинаем с чистого листа
    forgotten = await ui.api.delete("/api/ssh/known-hosts", params=where)
    assert forgotten.status_code == 200, forgotten.text
    # Свой sshd: отпечаток посчитан по его host.pub, как ssh-keygen -lf. Ниже воркер
    # должен показать ровно его
    real = forgotten.json()["fingerprint"]

    first = await ui.api.post("/api/ssh/sessions", json=ssh_target.connection())
    if first.status_code == 201:
        pytest.skip("this synex_worker build does not check the server key (before 2026-11-14)")
    assert first.status_code == 409, first.text
    assert first.json()["error"] == "ssh_host_key_unknown"
    host_key = first.json()["host_key"]
    assert (host_key["fingerprint"], host_key["host"], host_key["port"]) == (
        real,
        ssh_target.host,
        ssh_target.port,
    )
    assert host_key["key_type"] == "ssh-ed25519" or ssh_target.host_key_fingerprint is None
    assert (await ui.api.get("/api/ssh/sessions")).json() == []

    # Сохранён чужой отпечаток (как если бы ключ сервера сменился): подключения нет
    await ui.api.post("/api/ssh/known-hosts", json={**where, "fingerprint": WRONG})
    changed = await ui.api.post("/api/ssh/sessions", json=ssh_target.connection())
    assert changed.status_code == 409, changed.text
    assert changed.json()["error"] == "ssh_host_key_mismatch"
    assert changed.json()["host_key"]["expected_fingerprint"] == WRONG
    assert changed.json()["host_key"]["fingerprint"] == real
    assert (await ui.api.get("/api/ssh/sessions")).json() == []

    # Новый ключ сохраняется только явным действием пользователя
    conflict = await ui.api.post("/api/ssh/known-hosts", json={**where, "fingerprint": real})
    assert conflict.json()["error"] == "ssh_known_host_conflict"
    replaced = await ui.api.post(
        "/api/ssh/known-hosts",
        json={**where, "fingerprint": real, "key_type": host_key["key_type"], "replace": True},
    )
    assert replaced.status_code == 201, replaced.text

    opened = await ui.api.post("/api/ssh/sessions", json=ssh_target.connection())
    assert opened.status_code == 201, opened.text
    session = opened.json()
    assert session["host_key"]["fingerprint"] == real
    assert (await ui.exec(session["session_id"], "echo ok"))["stdout"] == "ok"

    # Служебная сессия (файлы) идёт с тем же ключом
    listing = await ui.api.get(
        f"/api/sftp/sessions/{session['session_id']}/list", params={"path": "/"}
    )
    assert listing.status_code == 200, listing.text
    assert (await ui.api.delete(f"/api/ssh/sessions/{session['session_id']}")).status_code == 200


async def test_server_known_to_openssh_needs_no_confirmation(
    ui: UI, ssh_target: SSHTarget, tmp_path: Path, monkeypatch
):
    """Сервер записан в known_hosts OpenSSH (как после обычного `ssh`): backend находит
    там ключ, который показал сервер, и подключается без вопроса. Другой ключ того же
    типа в этом файле значит, что ключ сервера сменился."""
    if ssh_target.host_public_key is None:
        pytest.skip("the host key of an external server is not known in advance")
    where = {"host": ssh_target.host, "port": ssh_target.port}
    name = f"[{ssh_target.host}]:{ssh_target.port}"
    known_hosts = tmp_path / "known_hosts"
    monkeypatch.setattr(settings, "OPENSSH_KNOWN_HOSTS_FILES", str(known_hosts))
    forgotten = await ui.api.delete("/api/ssh/known-hosts", params=where)
    assert forgotten.status_code == 200, forgotten.text

    known_hosts.write_text(f"other.example,{name} {ssh_target.host_public_key}\n")
    opened = await ui.api.post("/api/ssh/sessions", json=ssh_target.connection())
    assert opened.status_code == 201, opened.text
    session = opened.json()
    if session["host_key"] is None:
        pytest.skip("this synex_worker build does not check the server key (before 2026-11-14)")
    assert session["host_key"]["fingerprint"] == ssh_target.host_key_fingerprint
    [known] = (await ui.api.get("/api/ssh/known-hosts")).json()
    assert (known["fingerprint"], known["source"]) == (ssh_target.host_key_fingerprint, "openssh")
    assert (await ui.exec(session["session_id"], "echo ok"))["stdout"] == "ok"
    assert (await ui.api.delete(f"/api/ssh/sessions/{session['session_id']}")).status_code == 200

    # В файле OpenSSH у сервера другой ключ того же типа
    key_type = ssh_target.host_public_key.split()[0]
    type_name = key_type.encode()
    other_key = (
        len(type_name).to_bytes(4, "big") + type_name + (32).to_bytes(4, "big") + os.urandom(32)
    )
    await ui.api.delete("/api/ssh/known-hosts", params=where)
    known_hosts.write_text(f"{name} {key_type} {base64.b64encode(other_key).decode()}\n")
    changed = await ui.api.post("/api/ssh/sessions", json=ssh_target.connection())
    assert changed.status_code == 409, changed.text
    assert changed.json()["error"] == "ssh_host_key_mismatch"
    host_key = changed.json()["host_key"]
    assert (host_key["fingerprint"], host_key["source"]) == (
        ssh_target.host_key_fingerprint,
        "openssh",
    )
    assert host_key["expected_fingerprint"] != ssh_target.host_key_fingerprint
    assert (await ui.api.get("/api/ssh/known-hosts")).json() == []
    assert (await ui.api.get("/api/ssh/sessions")).json() == []
