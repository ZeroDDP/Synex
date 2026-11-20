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
from app.plugins.sftp.transfers import ACTIVE
from tests.conftest import UNICODE_FILE_NAMES
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
            if info["status"] not in ACTIVE:
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
            while not progress or progress[-1]["status"] in ACTIVE:
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


def write_tree(root: Path, content: dict[str, bytes | None]) -> None:
    """content: путь от root -> содержимое файла или None у папки."""
    root.mkdir(parents=True)
    for rel, data in content.items():
        path = root.joinpath(*rel.split("/"))
        if data is None:
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)


def read_tree(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): None if path.is_dir() else path.read_bytes()
        for path in sorted(root.rglob("*"))
    }


async def test_folder_goes_to_the_server_and_back(ui: UI, workspace, tmp_path: Path):
    """Папка целиком: вложенные папки, пустая папка, имена с пробелами, кавычками и
    кириллицей, сотня файлов. На сервер, обратно, объединение с существующей."""
    session_id, base, directory = workspace
    content: dict[str, bytes | None] = {
        "README.md": b"# project\n",
        "empty.txt": b"",
        "empty dir": None,
        "big/data.bin": os.urandom(3 * MB + 17),
        "docs/it's a 'quoted' name (v2).txt": b"quotes",
        "docs/with $dollar & amp; semicolon.txt": b"shell characters",
        "a/b/c/d/deep.txt": b"deep",
    }
    if UNICODE_FILE_NAMES:  # с локалью C и PYTHONUTF8=0 такой файл здесь не создать
        content["docs/отчёт за май (v2) 'final'.txt"] = b"report"
    content.update({f"many/part-{i:03}.txt": f"part {i}\n".encode() * i for i in range(100)})
    files_total = sum(1 for data in content.values() if data is not None)
    bytes_total = sum(len(data) for data in content.values() if data is not None)
    source = tmp_path / "project"
    write_tree(source, content)
    expected = read_tree(source)

    # ----------------------------------------------------------- на сервер
    async with ui.events() as events:
        started = time.monotonic()
        up = await ui.api.post(
            f"{base}/transfers",
            json={"direction": "upload", "local_path": str(source), "remote_path": directory},
        )
        assert up.status_code == 202, up.text
        assert (up.json()["kind"], up.json()["remote_path"]) == ("dir", f"{directory}/project")
        terminal = asyncio.ensure_future(ui.exec(session_id, "echo terminal-works"))
        progress: list[dict] = []
        async with asyncio.timeout(300):
            while not progress or progress[-1]["status"] in ACTIVE:
                message = await events.next(timeout=300)
                if message["type"] == "sftp.transfer" and message["data"]["id"] == up.json()["id"]:
                    progress.append(message["data"])
        upload_seconds = time.monotonic() - started
        assert (await terminal)["stdout"] == "terminal-works"
    final = progress[-1]
    assert (final["status"], final["skipped"]) == ("completed", []), final
    assert (final["files_done"], final["files_total"]) == (files_total, files_total)
    assert (final["bytes_done"], final["bytes_total"], final["percent"]) == (
        bytes_total,
        bytes_total,
        100.0,
    )
    statuses = [p["status"] for p in progress]
    assert statuses[:2] == ["queued", "scanning"] and "running" in statuses
    assert [p["bytes_done"] for p in progress] == sorted(p["bytes_done"] for p in progress)
    checksum = (await ui.api.get(f"{base}/info")).json()["checksum"]
    assert final["verified"] is (True if checksum else None)

    listing = (await ui.api.get(f"{base}/list", params={"path": directory})).json()
    assert [e["name"] for e in listing["entries"]] == ["project"]  # временной папки нет
    counted = await ui.exec(
        session_id,
        f"cd {directory}/project && find . -type f | wc -l && find . -type d | wc -l",
    )
    dirs_total = sum(1 for data in expected.values() if data is None) + 1  # и сама папка
    assert counted["stdout"].split() == [str(files_total), str(dirs_total)]

    # ------------------------------------------------------------- обратно
    started = time.monotonic()
    down = await ui.api.post(
        f"{base}/transfers",
        json={
            "direction": "download",
            "local_path": str(tmp_path / "back"),
            "remote_path": f"{directory}/project",
        },
    )
    assert down.status_code == 202, down.text
    info = await wait_transfer(ui, down.json()["id"], 300)
    download_seconds = time.monotonic() - started
    assert (info["status"], info["skipped"]) == ("completed", []), info
    assert (info["files_done"], info["bytes_done"]) == (files_total, bytes_total)
    assert read_tree(tmp_path / "back") == expected
    assert not list(tmp_path.glob("*.synex-part"))  # временной папки нет

    # ------------------------------------------ объединение и то, что пропускается
    (source / "README.md").write_bytes(b"# project, second edition\n")
    (source / "new" / "added.txt").parent.mkdir()
    (source / "new" / "added.txt").write_bytes(b"added")
    prepared = await ui.exec(
        session_id,
        f"cd {directory}/project && echo only-here > server-only.txt && ln -s /etc etc-link",
    )
    assert prepared["exit_code"] == 0, prepared
    body = {"direction": "upload", "local_path": str(source), "remote_path": directory}
    refused = await ui.api.post(f"{base}/transfers", json=body)
    assert (refused.status_code, refused.json()["error"]) == (409, "sftp_target_exists")
    merge = await ui.api.post(f"{base}/transfers", json={**body, "overwrite": True})
    assert merge.status_code == 202, merge.text
    merged = await wait_transfer(ui, merge.json()["id"], 300)
    assert (merged["status"], merged["skipped"]) == ("completed", []), merged
    assert merged["files_done"] == files_total + 1
    shown = await ui.exec(
        session_id,
        f"cd {directory}/project && cat README.md server-only.txt new/added.txt "
        "&& ls -a | grep -c synex-",
    )
    assert shown["stdout"].splitlines() == [
        "# project, second edition",
        "only-here",
        "added0",  # и ни одного временного файла
    ]

    again = await ui.api.post(
        f"{base}/transfers",
        json={
            "direction": "download",
            "local_path": str(tmp_path / "second"),
            "remote_path": f"{directory}/project",
        },
    )
    assert again.status_code == 202, again.text
    second = await wait_transfer(ui, again.json()["id"], 300)
    assert second["status"] == "completed", second
    assert second["skipped"] == [{"path": "etc-link", "reason": "Links are not transferred"}]
    assert (tmp_path / "second" / "server-only.txt").read_bytes() == b"only-here\n"
    assert (tmp_path / "second" / "README.md").read_bytes() == b"# project, second edition\n"
    assert not (tmp_path / "second" / "etc-link").exists()
    assert not list(tmp_path.rglob("*.synex-part"))

    megabytes = bytes_total / MB
    print(
        f"\nSFTP folder: {files_total} files, {megabytes:.1f} MB up in {upload_seconds:.1f}s, "
        f"down in {download_seconds:.1f}s"
    )


async def wait_current(ui: UI, transfer_id: str, name: str) -> None:
    """Дождаться, когда передача папки дойдёт до файла name."""
    async with asyncio.timeout(120):
        while True:
            info = (await ui.api.get(f"/api/sftp/transfers/{transfer_id}")).json()
            assert info["status"] in ACTIVE, info
            if info["current"] == name:
                return
            await asyncio.sleep(0.05)


async def test_cancelled_folder_leaves_nothing_behind(ui: UI, workspace, tmp_path: Path):
    """Отмена посреди большого файла: папки на месте нет, временная папка убрана (на
    сервере и на этом компьютере)."""
    session_id, base, directory = workspace
    big = 400 * MB  # файл «с дырой»: создаётся сразу, а передаётся несколько секунд

    # ----------------------------------------------------------- на сервер
    source = tmp_path / "upload-me"
    source.mkdir()
    (source / "a-small.txt").write_bytes(b"small")
    with (source / "b-big.bin").open("wb") as file:
        file.truncate(big)
    up = await ui.api.post(
        f"{base}/transfers",
        json={"direction": "upload", "local_path": str(source), "remote_path": directory},
    )
    assert up.status_code == 202, up.text
    await wait_current(ui, up.json()["id"], "b-big.bin")
    cancelled = await ui.api.delete(f"/api/sftp/transfers/{up.json()['id']}")
    assert cancelled.status_code == 200, cancelled.text
    info = await wait_transfer(ui, up.json()["id"], 60)
    assert (info["status"], info["error"]) == ("cancelled", "sftp_transfer_cancelled"), info
    async with asyncio.timeout(120):
        while (await ui.api.get(f"{base}/list", params={"path": directory})).json()["entries"]:
            await asyncio.sleep(0.2)

    # Сессия после отмены работает: следующая передача идёт сразу
    (source / "b-big.bin").unlink()
    again = await ui.api.post(
        f"{base}/transfers",
        json={"direction": "upload", "local_path": str(source), "remote_path": directory},
    )
    assert again.status_code == 202, again.text
    assert (await wait_transfer(ui, again.json()["id"], 60))["status"] == "completed"

    # ------------------------------------------------------------- обратно
    made = await ui.exec(
        session_id,
        f"dd if=/dev/zero of={directory}/upload-me/b-big.bin bs=1048576 count=0 seek=400",
    )
    assert made["exit_code"] == 0, made
    down = await ui.api.post(
        f"{base}/transfers",
        json={
            "direction": "download",
            "local_path": str(tmp_path / "down"),
            "remote_path": f"{directory}/upload-me",
        },
    )
    assert down.status_code == 202, down.text
    await wait_current(ui, down.json()["id"], "b-big.bin")
    await ui.api.delete(f"/api/sftp/transfers/{down.json()['id']}")
    info = await wait_transfer(ui, down.json()["id"], 60)
    assert (info["status"], info["error"]) == ("cancelled", "sftp_transfer_cancelled"), info
    async with asyncio.timeout(120):
        while list(tmp_path.glob("*.synex-part")):
            await asyncio.sleep(0.2)
    assert not (tmp_path / "down").exists()


async def test_search_on_the_server(ui: UI, workspace):
    """Поиск вкладки Files: find на сервере в своём SSH-подключении."""
    session_id, base, directory = workspace
    created = await ui.exec(
        session_id,
        f"cd {directory} && mkdir -p conf/nginx .cache && printf 'listen 80;\\n' > "
        "conf/nginx/site.conf && printf 'x' > conf/app.log && printf 'listen' > .cache/x.conf",
    )
    assert created["exit_code"] == 0, created

    by_name = await ui.api.get(f"{base}/search", params={"path": directory, "name": "*.conf"})
    assert by_name.status_code == 200, by_name.text
    assert [e["path"] for e in by_name.json()["entries"]] == [f"{directory}/conf/nginx/site.conf"]

    inside = await ui.api.get(
        f"{base}/search", params={"path": directory, "contains": "LISTEN", "hidden": "true"}
    )
    assert inside.status_code == 200, inside.text
    assert sorted(e["name"] for e in inside.json()["entries"]) == ["site.conf", "x.conf"]

    folders = await ui.api.get(
        f"{base}/search", params={"path": directory, "name": "nginx", "type": "dir"}
    )
    (entry,) = folders.json()["entries"]
    assert (entry["name"], entry["parent"]) == ("nginx", f"{directory}/conf")
