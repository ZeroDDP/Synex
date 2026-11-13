"""Плагины полностью: UI → FastAPI → synex_worker → SSH-сервер → git, docker на сервере.

Все плагины включены (PLUGINS_ENABLED=*), как у пользователя по умолчанию. Тесты git и
docker готовят репозиторий и контейнер прямо на SSH-сервере, поэтому идут только с
локальным sshd (не с внешним SYNEX_TEST_SSH_*); docker нужен работающий.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import uuid
from pathlib import Path

import pytest

from app.core.config import settings
from tests.integration.sshd import SSHTarget
from tests.integration.ui import UI

LOCAL_SERVER = not os.environ.get("SYNEX_TEST_SSH_HOST")


@pytest.fixture(autouse=True)
def all_plugins(monkeypatch):
    monkeypatch.setattr(settings, "PLUGINS_ENABLED", "*")


async def test_all_plugins_are_loaded(ui: UI):
    plugins = {p["name"]: p for p in (await ui.api.get("/api/plugins")).json()}
    for name in ("ssh", "keys", "monitor", "connections", "docker", "git", "sftp"):
        assert plugins[name]["status"] == "loaded", plugins[name]
    assert plugins["docker"]["requires"] == ["ssh"]


async def test_saved_connection_full_cycle(ui: UI, ssh_target: SSHTarget):
    """Сохранённое подключение → сессия из него → команда; UI видит connection_id."""
    body = {
        "name": f"it-{uuid.uuid4().hex[:8]}",
        "host": ssh_target.host,
        "port": ssh_target.port,
        "username": ssh_target.username,
        "auth": "key_path",
        "key_path": ssh_target.key_path,
    }
    saved = await ui.api.post("/api/connections", json=body)
    assert saved.status_code == 201, saved.text
    connection_id = saved.json()["id"]

    async with ui.events() as events:
        opened = await ui.api.post(f"/api/connections/{connection_id}/connect")
        assert opened.status_code == 201, opened.text
        session = opened.json()
        assert session["connection_id"] == connection_id
        event = await events.wait_for(
            "ssh.session", lambda data: data["session_id"] == session["session_id"]
        )
        assert event["data"]["connection_id"] == connection_id

    whoami = await ui.exec(session["session_id"], "whoami")
    assert whoami["stdout"] == ssh_target.username
    info = (await ui.api.get(f"/api/connections/{connection_id}")).json()
    assert info["last_connected_at"] is not None
    await ui.api.delete(f"/api/connections/{connection_id}")


# ---------------------------------------------------------------------- git

needs_local_git = pytest.mark.skipif(
    not LOCAL_SERVER or not shutil.which("git"), reason="needs local sshd and git"
)


def git(*args: str, cwd: Path) -> None:
    identity = ["-c", "user.name=Test", "-c", "user.email=t@e", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", *identity, *args], cwd=cwd, check=True, capture_output=True)


@needs_local_git
async def test_git_full_cycle(ui: UI, ssh_target: SSHTarget, tmp_path: Path):
    """Репозиторий на сервере отстаёт от origin: status, fetch, pull и деплой."""
    git("init", "-q", "--bare", "-b", "main", "origin.git", cwd=tmp_path)
    git("clone", "-q", "origin.git", "app", cwd=tmp_path)
    git("clone", "-q", "origin.git", "dev", cwd=tmp_path)
    app, dev = tmp_path / "app", tmp_path / "dev"
    (dev / "version").write_text("1\n")
    git("add", "version", cwd=dev)
    git("commit", "-q", "-m", "Release 1", cwd=dev)
    git("push", "-q", "origin", "HEAD:main", cwd=dev)
    git("pull", "-q", "origin", "main", cwd=app)
    git("branch", "-q", "-u", "origin/main", cwd=app)
    (dev / "version").write_text("2\n")
    git("commit", "-q", "-am", "Release 2", cwd=dev)
    git("push", "-q", "origin", "HEAD:main", cwd=dev)

    session_id = await ui.open_session(ssh_target.connection())
    base = f"/api/git/sessions/{session_id}"
    path = str(app)

    status = (await ui.api.get(f"{base}/status", params={"path": path})).json()
    assert (status["branch"], status["clean"]) == ("main", True)
    assert status["last_commit"]["subject"] == "Release 1"

    fetched = (await ui.api.post(f"{base}/fetch", json={"path": path})).json()
    assert (fetched["behind"], fetched["ahead"]) == (1, 0)

    deployed = await ui.api.post(
        f"{base}/deploy",
        json={"path": path, "command": "cat version && pwd", "only_if_updated": True},
    )
    assert deployed.status_code == 200, deployed.text
    body = deployed.json()
    assert body["ok"] is True
    assert [c["subject"] for c in body["pull"]["commits"]] == ["Release 2"]
    version, cwd = body["command"]["stdout"].splitlines()
    assert version == "2"
    assert Path(cwd).resolve() == app.resolve()

    # Терминал сессии служебных команд не видел, cwd сессии не изменилась
    output = (await ui.api.get(f"/api/ssh/sessions/{session_id}/output")).json()
    assert [e["kind"] for e in output["entries"]] == ["opened"]


# ------------------------------------------------------------------- docker


def docker_works() -> bool:
    if not LOCAL_SERVER or not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


@pytest.mark.skipif(not docker_works(), reason="needs local sshd and a running Docker")
async def test_docker_full_cycle(ui: UI, ssh_target: SSHTarget, tmp_path: Path):
    """Контейнер на сервере: список, логи, удаление. Образ собирается из пустого
    архива (docker import), чтобы не скачивать ничего из сети."""
    name = f"synex-it-{uuid.uuid4().hex[:8]}"
    image = f"{name}:test"
    archive = tmp_path / "empty.tar"
    with tarfile.open(archive, "w"):
        pass
    subprocess.run(["docker", "import", str(archive), image], check=True, capture_output=True)
    subprocess.run(
        ["docker", "create", "--name", name, "--label", "synex.test=1", image, "/none"],
        check=True,
        capture_output=True,
    )
    try:
        session_id = await ui.open_session(ssh_target.connection())
        base = f"/api/docker/sessions/{session_id}"

        info = (await ui.api.get(f"{base}/info")).json()
        assert info["status"] == "ok", info

        containers = (await ui.api.get(f"{base}/containers")).json()
        (created,) = [c for c in containers if c["name"] == name]
        assert (created["state"], created["image"], created["labels"]["synex.test"]) == (
            "created",
            image,
            "1",
        )
        logs = (await ui.api.get(f"{base}/containers/{name}/logs")).json()
        assert logs["lines"] == []
        assert any(i["repository"] == name for i in (await ui.api.get(f"{base}/images")).json())

        # Контейнер без программы не запускается: ошибка docker доходит до UI
        failed = await ui.api.post(f"{base}/containers/{name}/start")
        assert failed.status_code == 502, failed.text
        assert failed.json()["error"] == "docker_command_failed"

        removed = await ui.api.delete(f"{base}/containers/{name}")
        assert removed.status_code == 200, removed.text
        missing = await ui.api.get(f"{base}/containers/{name}/logs")
        assert missing.json()["error"] == "docker_container_not_found"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "rmi", image], capture_output=True)
