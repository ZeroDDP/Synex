"""Файловый менеджер полностью: UI → FastAPI → synex_worker → SSH-сервер.

Критерий MVP по SFTP: файл не меньше 100 МБ уходит на сервер и возвращается без ошибок.
Размер файла задаёт SYNEX_TEST_SFTP_MB (по умолчанию 100).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
import uuid
from pathlib import Path

import pytest

from app.core.config import settings
from tests.integration.sshd import SSHTarget
from tests.integration.ui import UI

BIG_FILE_MB = int(os.environ.get("SYNEX_TEST_SFTP_MB", "100"))
MB = 1024 * 1024


@pytest.fixture(autouse=True)
def all_plugins(monkeypatch):
    monkeypatch.setattr(settings, "PLUGINS_ENABLED", "*")


@pytest.fixture
async def workspace(ui: UI, ssh_target: SSHTarget):
    """Сессия и пустая папка на сервере, которая удаляется после теста."""
    session_id = await ui.open_session(ssh_target.connection())
    base = f"/api/sftp/sessions/{session_id}"
    directory = f"/tmp/synex-it-{uuid.uuid4().hex[:12]}"
    made = await ui.api.post(f"{base}/mkdir", json={"path": directory})
    assert made.status_code == 201, made.text
    try:
        yield session_id, base, directory
    finally:
        await ui.api.delete(f"{base}/entry", params={"path": directory, "recursive": "true"})


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(MB):
            digest.update(chunk)
    return digest.hexdigest()


def write_random_file(path: Path, megabytes: int) -> None:
    with path.open("wb") as file:
        for _ in range(megabytes):
            file.write(os.urandom(MB))
        file.write(os.urandom(4321))  # размер не кратен куску


async def wait_transfer(ui: UI, transfer_id: str, timeout: float) -> dict:
    async with asyncio.timeout(timeout):
        while True:
            info = (await ui.api.get(f"/api/sftp/transfers/{transfer_id}")).json()
            if info["status"] not in ("queued", "running", "verifying"):
                return info
            await asyncio.sleep(0.2)


async def test_file_manager_full_cycle(ui: UI, workspace, ssh_target: SSHTarget):
    """Папки, файлы с необычными именами, загрузка и скачивание телом запроса."""
    session_id, base, directory = workspace

    info = (await ui.api.get(f"{base}/info")).json()
    assert (info["engine"], info["user"]) == ("core", ssh_target.username), info
    assert info["home"].startswith("/")

    name = "отчёт за май (v2) 'final'.bin"
    data = os.urandom(300_000) + bytes(range(256))
    uploaded = await ui.api.post(
        f"{base}/upload", params={"path": f"{directory}/{name}"}, content=data
    )
    assert uploaded.status_code == 201, uploaded.text
    assert (uploaded.json()["status"], uploaded.json()["bytes_done"]) == ("completed", len(data))
    assert uploaded.json()["checksum"] == hashlib.sha256(data).hexdigest()
    if (await ui.api.get(f"{base}/info")).json()["checksum"]:
        assert uploaded.json()["verified"] is True

    listing = (await ui.api.get(f"{base}/list", params={"path": directory})).json()
    assert [(e["name"], e["size"], e["type"]) for e in listing["entries"]] == [
        (name, len(data), "file")
    ]
    assert listing["parent"] == "/tmp"

    made = await ui.api.post(f"{base}/mkdir", json={"path": f"{directory}/a/b", "parents": True})
    assert made.status_code == 201, made.text
    moved = await ui.api.post(
        f"{base}/move",
        json={"source": f"{directory}/{name}", "target": f"{directory}/a/b/moved.bin"},
    )
    assert moved.status_code == 200, moved.text
    copied = await ui.api.post(
        f"{base}/copy", json={"source": f"{directory}/a", "target": f"{directory}/copy"}
    )
    assert copied.status_code == 201, copied.text

    downloaded = await ui.api.get(
        f"{base}/download", params={"path": f"{directory}/copy/b/moved.bin"}
    )
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == data
    assert downloaded.headers["content-length"] == str(len(data))

    missing = await ui.api.get(f"{base}/download", params={"path": f"{directory}/none.bin"})
    assert (missing.status_code, missing.json()["error"]) == (404, "sftp_not_found")
    not_empty = await ui.api.delete(f"{base}/entry", params={"path": f"{directory}/a"})
    assert (not_empty.status_code, not_empty.json()["error"]) == (409, "sftp_directory_not_empty")
    removed = await ui.api.delete(
        f"{base}/entry", params={"path": f"{directory}/a", "recursive": "true"}
    )
    assert removed.status_code == 200, removed.text
    names = [
        e["name"]
        for e in (await ui.api.get(f"{base}/list", params={"path": directory})).json()["entries"]
    ]
    assert names == ["copy"]

    # Терминал сессии файлового менеджера не видел, cwd сессии не изменилась
    output = (await ui.api.get(f"/api/ssh/sessions/{session_id}/output")).json()
    assert [e["kind"] for e in output["entries"]] == ["opened"]


async def test_big_listing_keeps_names_that_are_not_ascii(ui: UI, workspace):
    """Список на полторы тысячи файлов с русскими именами приходит целым. (Вывод команд
    воркер переводит в текст по пакетам SSH и портит букву на границе пакетов; список
    SFTP идёт отдельно от вывода команд, ему это не грозит.)"""
    session_id, base, directory = workspace
    pattern = "файл с русским именем %04d и ещё немного текста.txt"
    count = 1500
    created = await ui.exec(
        session_id,
        f"cd {directory} && i=0 && while [ $i -lt {count} ]; do "
        f": > \"$(printf '{pattern}' $i)\"; i=$((i+1)); done",
    )
    assert created["exit_code"] == 0, created

    listing = await ui.api.get(f"{base}/list", params={"path": directory})
    assert listing.status_code == 200, listing.text
    names = [entry["name"] for entry in listing.json()["entries"]]
    assert names == [pattern % i for i in range(count)]


async def test_big_file_goes_to_the_server_and_back(ui: UI, workspace, tmp_path: Path):
    """Файл ≥100 МБ: на сервер и обратно, SHA-256 совпадает, прогресс идёт в /ws, а
    терминал сессии и метрики во время передачи продолжают работать (передача идёт в
    служебной сессии)."""
    session_id, base, directory = workspace
    source = tmp_path / "big-source.bin"
    result = tmp_path / "big-result.bin"
    write_random_file(source, BIG_FILE_MB)
    size = source.stat().st_size
    expected = sha256_of(source)
    remote = f"{directory}/big.bin"
    limit = 120 + BIG_FILE_MB * 6  # сек на одну передачу

    async with ui.events() as events:
        # ------------------------------------------------------- на сервер
        started = time.monotonic()
        up = await ui.api.post(
            f"{base}/transfers",
            json={"direction": "upload", "local_path": str(source), "remote_path": remote},
        )
        assert up.status_code == 202, up.text
        upload_id = up.json()["id"]

        # Команда терминала во время передачи: основная сессия свободна
        terminal = asyncio.ensure_future(ui.exec(session_id, "echo terminal-works"))
        progress: list[dict] = []
        async with asyncio.timeout(limit):
            while not progress or progress[-1]["status"] in ("queued", "running", "verifying"):
                message = await events.next(timeout=limit)
                if message["type"] == "sftp.transfer" and message["data"]["id"] == upload_id:
                    progress.append(message["data"])
        upload_seconds = time.monotonic() - started
        assert (await terminal)["stdout"] == "terminal-works"

        final = progress[-1]
        assert final["status"] == "completed", final
        assert (final["bytes_done"], final["bytes_total"], final["percent"]) == (size, size, 100.0)
        assert final["checksum"] == expected
        statuses = [p["status"] for p in progress]
        assert statuses[0] == "queued" and "running" in statuses and "verifying" in statuses
        done = [p["bytes_done"] for p in progress]
        assert done == sorted(done)

        stat = (await ui.api.get(f"{base}/stat", params={"path": remote})).json()
        assert stat["size"] == size

        # ---------------------------------------------------------- обратно
        started = time.monotonic()
        down = await ui.api.post(
            f"{base}/transfers",
            json={"direction": "download", "local_path": str(result), "remote_path": remote},
        )
        assert down.status_code == 202, down.text
        info = await wait_transfer(ui, down.json()["id"], limit)
        download_seconds = time.monotonic() - started

    assert info["status"] == "completed", info
    assert (info["bytes_done"], info["checksum"]) == (size, expected)
    assert result.stat().st_size == size
    assert sha256_of(result) == expected
    assert not list(result.parent.glob("*.synex-part"))
    if (await ui.api.get(f"{base}/info")).json()["checksum"]:
        assert final["verified"] is True and info["verified"] is True

    megabytes = size / MB
    print(
        f"\nSFTP cycle: {megabytes:.0f} MB up in {upload_seconds:.1f}s "
        f"({megabytes / upload_seconds:.1f} MB/s), down in {download_seconds:.1f}s "
        f"({megabytes / download_seconds:.1f} MB/s), SHA-256 matches"
    )
