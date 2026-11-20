"""Передача папок целиком (app/plugins/sftp/folders.py) через HTTP API. Сервер здесь
фейковый воркер с эмулятором shell, как в tests/test_sftp_api.py; с настоящим воркером и
sshd то же проверяет tests/integration/test_sftp_cycle.py."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.core.config import settings
from app.plugins.sftp import folders, local, transfers
from tests.fake_shell import HOME
from tests.sftp_rig import Files, files_rig

MB = 1024 * 1024
not_windows = pytest.mark.skipif(
    os.name == "nt", reason="the fake server keeps its files on this disk"
)


@pytest.fixture(params=["2026-11-14", "2026-11-12"])
async def files(request, tmp_path: Path, missing_socket_path, monkeypatch) -> AsyncIterator[Files]:
    async with files_rig(tmp_path, missing_socket_path, monkeypatch, request.param) as rig:
        yield rig


def make_tree(root: Path, content: dict[str, bytes | None]) -> None:
    """content: путь от root -> содержимое файла или None у папки."""
    root.mkdir(parents=True, exist_ok=True)
    for rel, data in content.items():
        path = root.joinpath(*rel.split("/"))
        if data is None:
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)


def snapshot(root: Path) -> dict[str, bytes | None]:
    """Что лежит в папке: путь от root -> содержимое файла или None у папки."""
    return {
        path.relative_to(root).as_posix(): None if path.is_dir() else path.read_bytes()
        for path in sorted(root.rglob("*"))
    }


def with_parents(content: dict[str, bytes | None]) -> dict[str, bytes | None]:
    """content и все папки на пути к его файлам: так его показывает snapshot."""
    result = dict(content)
    for rel in content:
        parts = rel.split("/")
        for depth in range(1, len(parts)):
            result.setdefault("/".join(parts[:depth]), None)
    return dict(sorted(result.items()))


PROJECT: dict[str, bytes | None] = {
    "README.md": b"# project\n",
    "empty.txt": b"",
    "src/main.py": b"print('hi')\n" * 100,
    "src/pkg/data.bin": bytes(range(256)) * 300,
    "src/pkg/deep/more/notes with spaces.txt": b"notes",
    "assets": None,  # пустая папка тоже передаётся
}
PROJECT_FILES = sum(1 for data in PROJECT.values() if data is not None)
PROJECT_BYTES = sum(len(data) for data in PROJECT.values() if data is not None)


def temporary(root: Path) -> list[str]:
    """Временные файлы и папки передач, оставшиеся под root."""
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if ".synex-" in path.name or path.name.endswith(local.PART_SUFFIX)
    )


# ------------------------------------------------------------------- new folder


async def test_upload_a_new_folder(files: Files):
    make_tree(files.local / "project", PROJECT)
    started = await files.rig.api.post(
        f"{files.base}/transfers",
        json={
            "direction": "upload",
            "local_path": str(files.local / "project"),
            "remote_path": "~",
        },
    )
    assert started.status_code == 202, started.text
    first = started.json()
    assert (first["kind"], first["status"], first["name"]) == ("dir", "queued", "project")
    assert (first["remote_path"], first["local_path"]) == (
        f"{HOME}/project",
        str(files.local / "project"),
    )
    assert (first["bytes_total"], first["files_total"]) == (None, None)  # обхода ещё не было

    done = await files.finished(first["id"])
    assert (done["status"], done["error"], done["verified"]) == ("completed", None, True), done
    assert (done["files_done"], done["files_total"]) == (PROJECT_FILES, PROJECT_FILES)
    assert (done["bytes_done"], done["bytes_total"], done["percent"]) == (
        PROJECT_BYTES,
        PROJECT_BYTES,
        100.0,
    )
    assert (done["skipped"], done["skipped_count"], done["current"]) == ([], 0, None)
    assert snapshot(files.server("~/project")) == with_parents(PROJECT)
    assert [p.name for p in files.server("~").iterdir()] == ["project"]  # временного нет

    events = [e for e in files.events if e["id"] == first["id"]]
    statuses = [e["status"] for e in events]
    assert statuses[:2] == ["queued", "scanning"] and statuses[-1] == "completed"
    assert "running" in statuses
    assert [e["bytes_done"] for e in events] == sorted(e["bytes_done"] for e in events)
    named = {e["current"] for e in events} - {None}  # какой файл идёт (события не о каждом)
    assert named and named <= {rel for rel, data in PROJECT.items() if data is not None}

    # Вся папка шла в одной служебной сессии: основная была свободна
    senders = {r["id"] for r in files.requests("sftp_upload_file")}
    assert len(senders) == 1 and files.rig.session_id not in senders
    assert files.rig.manager.companions(files.rig.session_id) == []


async def test_download_a_new_folder(files: Files):
    make_tree(files.server("~/project"), PROJECT)
    done = await files.transfer(
        direction="download", local_path=str(files.local), remote_path="~/project"
    )
    assert (done["kind"], done["status"], done["verified"]) == ("dir", "completed", True), done
    assert (done["remote_path"], done["local_path"]) == (
        f"{HOME}/project",
        str(files.local / "project"),
    )
    assert (done["files_done"], done["files_total"]) == (PROJECT_FILES, PROJECT_FILES)
    assert (done["bytes_done"], done["bytes_total"]) == (PROJECT_BYTES, PROJECT_BYTES)
    assert snapshot(files.local / "project") == with_parents(PROJECT)
    assert [p.name for p in files.local.iterdir()] == ["project"]
    senders = {r["id"] for r in files.requests("sftp_download_file")}
    assert len(senders) == 1 and files.rig.session_id not in senders


async def test_a_folder_under_another_name(files: Files):
    """Путь, которого ещё нет, это имя новой папки (а существующая папка значит «в неё»)."""
    make_tree(files.local / "project", {"a.txt": b"a"})
    files.server("~/sites").mkdir()
    up = await files.transfer(
        direction="upload",
        local_path=str(files.local / "project"),
        remote_path="sites/shop",
    )
    assert (up["status"], up["remote_path"]) == ("completed", f"{HOME}/sites/shop"), up
    assert snapshot(files.server("~/sites")) == {"shop": None, "shop/a.txt": b"a"}

    down = await files.transfer(
        direction="download",
        local_path=str(files.local / "copy"),
        remote_path="~/sites/shop",
    )
    assert (down["status"], down["local_path"]) == ("completed", str(files.local / "copy")), down
    assert snapshot(files.local / "copy") == {"a.txt": b"a"}


async def test_an_empty_folder(files: Files):
    (files.local / "nothing").mkdir()
    done = await files.transfer(
        direction="upload", local_path=str(files.local / "nothing"), remote_path="~"
    )
    assert (done["status"], done["files_total"], done["bytes_total"]) == ("completed", 0, 0)
    assert (done["percent"], done["verified"]) == (100.0, None)  # сверять было нечего
    assert files.server("~/nothing").is_dir()


async def test_refusals_before_a_folder_transfer_starts(files: Files):
    files.put("~/file.bin", b"old")
    files.server("~/taken").mkdir()
    make_tree(files.local / "taken", {"a.txt": b"a"})
    (files.local / "file.bin").write_bytes(b"local")
    folder = str(files.local / "taken")
    cases = [
        ({"direction": "upload", "local_path": folder, "remote_path": "~"},
         409, "sftp_target_exists"),
        ({"direction": "upload", "local_path": folder, "remote_path": "file.bin"},
         409, "sftp_not_a_directory"),
        ({"direction": "upload", "local_path": folder, "remote_path": "no/such/"},
         404, "sftp_not_found"),
        ({"direction": "upload", "local_path": folder, "remote_path": "no/such/dir"},
         404, "sftp_not_found"),
        ({"direction": "download", "local_path": str(files.local), "remote_path": "taken"},
         409, "sftp_local_exists"),
        ({"direction": "download", "local_path": str(files.local / "file.bin"),
          "remote_path": "taken"}, 409, "sftp_local_not_a_directory"),
        ({"direction": "download", "local_path": str(files.local / "no" / "such"),
          "remote_path": "taken"}, 404, "sftp_local_not_found"),
    ]  # fmt: skip
    for body, status, code in cases:
        response = await files.rig.api.post(f"{files.base}/transfers", json=body)
        assert (response.status_code, response.json()["error"]) == (status, code), body
    assert "overwrite=true to merge" in response.json()["detail"] or status != 409
    # Телом ответа папка не скачивается
    response = await files.rig.api.get(f"{files.base}/download", params={"path": "taken"})
    assert (response.status_code, response.json()["error"]) == (409, "sftp_is_a_directory")
    assert (await files.rig.api.get("/api/sftp/transfers")).json() == []
    assert temporary(files.server("~")) == [] and temporary(files.local) == []


# ------------------------------------------------------- only a complete folder


async def test_a_new_folder_is_in_place_only_when_complete(files: Files):
    """Пока папка загружается, на месте её нет: она собирается во временной. Отмена
    убирает временную папку целиком."""
    files.rig.worker.sftp_delay = 0.2
    make_tree(
        files.local / "big",
        {"a.bin": os.urandom(MB), "sub/b.bin": os.urandom(8 * MB), "sub/c.bin": b"c"},
    )
    started = await files.rig.api.post(
        f"{files.base}/transfers",
        json={"direction": "upload", "local_path": str(files.local / "big"), "remote_path": "~"},
    )
    transfer_id = started.json()["id"]
    async with asyncio.timeout(10):
        while True:
            info = (await files.rig.api.get(f"/api/sftp/transfers/{transfer_id}")).json()
            if info["current"] == "sub/b.bin":
                break
            await asyncio.sleep(0.02)
    assert (info["status"], info["files_done"], info["files_total"]) == ("running", 1, 3)
    assert MB <= info["bytes_done"] < info["bytes_total"] and 0 < info["percent"] < 100
    assert [p.name for p in files.server("~").iterdir()] == [f".synex-{transfer_id}.part"]

    cancelled = await files.rig.api.delete(f"/api/sftp/transfers/{transfer_id}")
    assert cancelled.status_code == 200
    info = await files.finished(transfer_id)
    assert (info["status"], info["error"]) == ("cancelled", "sftp_transfer_cancelled")
    async with asyncio.timeout(5):  # уборка временной папки после обрыва
        while list(files.server("~").iterdir()):
            await asyncio.sleep(0.02)


async def test_a_cancelled_download_leaves_nothing(files: Files):
    files.rig.worker.sftp_delay = 0.2
    make_tree(files.server("~/big"), {"a.bin": os.urandom(MB), "b.bin": os.urandom(8 * MB)})
    started = await files.rig.api.post(
        f"{files.base}/transfers",
        json={"direction": "download", "local_path": str(files.local), "remote_path": "big"},
    )
    transfer_id = started.json()["id"]
    async with asyncio.timeout(10):
        while (await files.rig.api.get(f"/api/sftp/transfers/{transfer_id}")).json()[
            "files_done"
        ] < 1:
            await asyncio.sleep(0.02)
    assert [p.name for p in files.local.iterdir()] == [f"big.{transfer_id}.synex-part"]
    await files.rig.api.delete(f"/api/sftp/transfers/{transfer_id}")
    assert (await files.finished(transfer_id))["status"] == "cancelled"
    async with asyncio.timeout(5):
        while list(files.local.iterdir()):
            await asyncio.sleep(0.02)


async def test_a_failure_in_the_middle_removes_the_new_folder(files: Files):
    make_tree(files.local / "site", {"a.txt": b"a", "b.txt": b"b", "c/d.txt": b"d"})

    def refuse(action: str, payload: dict) -> str | None:
        if action == "sftp_upload_file" and str(payload.get("remote_path")).endswith("/b.txt"):
            return "SSH internal error: Failed to upload file: Failure"
        return None

    files.rig.worker.sftp_fail_if = refuse
    info = await files.transfer(
        direction="upload", local_path=str(files.local / "site"), remote_path="~"
    )
    assert (info["status"], info["error"]) == ("failed", "sftp_failed"), info
    assert list(files.server("~").iterdir()) == []


async def test_closing_the_session_stops_a_folder_transfer(files: Files):
    files.rig.worker.sftp_delay = 0.2
    make_tree(files.local / "big", {"a.bin": os.urandom(8 * MB), "b.bin": b"b"})
    started = await files.rig.api.post(
        f"{files.base}/transfers",
        json={"direction": "upload", "local_path": str(files.local / "big"), "remote_path": "~"},
    )
    transfer_id = started.json()["id"]
    async with asyncio.timeout(10):
        while (await files.rig.api.get(f"/api/sftp/transfers/{transfer_id}")).json()[
            "status"
        ] != "running":
            await asyncio.sleep(0.02)
    await files.rig.manager.close(files.rig.session_id)
    await files.service.on_session_event({"action": "closed", "session_id": files.rig.session_id})
    info = await files.finished(transfer_id)
    assert info["status"] in ("cancelled", "failed")
    assert files.rig.manager.companions(files.rig.session_id) == []
    assert not files.server("~/big").exists()


# ------------------------------------------ files the worker creates after a cancel
#
# Настоящий воркер не прерывает передачу, подключение которой закрыли: он доделывает её в
# фоне. Если отмена пришла в самом начале файла, воркер создаёт его уже после того, как
# backend убрал временные файлы. Такой файл убирается следом.


@pytest.fixture
def late_worker(files: Files, monkeypatch) -> Files:
    """Фейковый воркер ведёт себя так же: файл создаёт через 0,5 с после того, как
    подключение закрыли (отмена пришла в самом начале файла), и дописывает его."""
    files.rig.worker.sftp_zombie = True
    files.rig.worker.sftp_start_after_loss = True
    files.rig.worker.sftp_start_delay = 0.5
    monkeypatch.setattr(transfers, "SWEEP_PAUSES", (0.05,) * 60)
    monkeypatch.setattr(transfers, "CLEANUP_PAUSE", 0.05)
    monkeypatch.setattr(transfers, "LATE_SECONDS", 3.0)
    return files


async def cancel_at_the_start_of_a_file(files: Files, action: str, **body: object) -> dict:
    """Начать передачу и отменить её, как только воркер получил запрос action (файла он
    ещё не создал)."""
    started = await files.rig.api.post(f"{files.base}/transfers", json=body)
    assert started.status_code == 202, started.text
    async with asyncio.timeout(10):
        while not files.requests(action):
            await asyncio.sleep(0.005)
    await files.rig.api.delete(f"/api/sftp/transfers/{started.json()['id']}")
    info = await files.finished(started.json()["id"])
    assert (info["status"], info["error"]) == ("cancelled", "sftp_transfer_cancelled"), info
    return info


async def late_file(files: Files) -> Path:
    """Файл, который воркер дописал уже после отмены."""
    async with asyncio.timeout(10):
        while not files.rig.worker.sftp_finished_late:
            await asyncio.sleep(0.01)
    return Path(files.rig.worker.sftp_finished_late[0])


async def gone(path: Path) -> None:
    async with asyncio.timeout(5):
        while path.exists():
            await asyncio.sleep(0.02)


async def test_a_file_created_on_the_server_after_the_cancel_is_removed(late_worker: Files):
    """Объединение папок: временный файл, который воркер создал после отмены, не остаётся
    в папке пользователя."""
    files = late_worker
    make_tree(files.local / "site", {"a.txt": b"new a"})
    make_tree(files.server("~/site"), {"a.txt": b"old a", "keep.txt": b"keep"})
    await cancel_at_the_start_of_a_file(
        files,
        "sftp_upload_file",
        direction="upload",
        local_path=str(files.local / "site"),
        remote_path="~",
        overwrite=True,
    )
    assert temporary(files.server("~/site")) == []  # к концу отмены воркер его ещё не создал
    stray = await late_file(files)
    assert stray.parent == files.server("~/site") and ".synex-" in stray.name
    await gone(stray)
    assert snapshot(files.server("~/site")) == {"a.txt": b"old a", "keep.txt": b"keep"}
    # Служебная сессия уборки закрыта
    async with asyncio.timeout(5):
        while files.rig.manager.companions(files.rig.session_id):
            await asyncio.sleep(0.02)


async def test_late_files_are_removed_in_the_main_session_too(late_worker: Files):
    """Вторую служебную сессию для уборки открыть не вышло: проверки идут в основной."""
    files = late_worker
    make_tree(files.local / "site", {"a.txt": b"new a"})
    make_tree(files.server("~/site"), {"keep.txt": b"keep"})
    open_companion = files.rig.manager.open_companion
    opened = 0

    async def only_once(session_id: str, purpose: str):
        nonlocal opened
        opened += 1
        return await open_companion(session_id, purpose) if opened == 1 else None

    files.rig.manager.open_companion = only_once  # type: ignore[method-assign]
    await cancel_at_the_start_of_a_file(
        files,
        "sftp_upload_file",
        direction="upload",
        local_path=str(files.local / "site"),
        remote_path="~",
        overwrite=True,
    )
    await gone(await late_file(files))
    assert opened == 2
    assert snapshot(files.server("~/site")) == {"keep.txt": b"keep"}


async def test_a_file_created_here_after_the_cancel_is_removed(late_worker: Files):
    """То же при скачивании: и в папку (объединение), и одного файла."""
    files = late_worker
    make_tree(files.server("~/site"), {"a.txt": b"new a"})
    make_tree(files.local / "site", {"a.txt": b"old a", "keep.txt": b"keep"})
    await cancel_at_the_start_of_a_file(
        files,
        "sftp_download_file",
        direction="download",
        local_path=str(files.local),
        remote_path="site",
        overwrite=True,
    )
    stray = await late_file(files)
    assert stray.parent == files.local / "site" and stray.name.endswith(local.PART_SUFFIX)
    await gone(stray)
    assert snapshot(files.local / "site") == {"a.txt": b"old a", "keep.txt": b"keep"}

    files.rig.worker.sftp_finished_late.clear()
    files.rig.worker.requests.clear()
    await cancel_at_the_start_of_a_file(
        files,
        "sftp_download_file",
        direction="download",
        local_path=str(files.local / "one.txt"),
        remote_path="site/a.txt",
    )
    stray = await late_file(files)
    assert stray.parent == files.local and stray.name.endswith(local.PART_SUFFIX)
    await gone(stray)
    assert temporary(files.local) == [] and not (files.local / "one.txt").exists()


async def test_a_new_folder_is_removed_even_if_the_first_try_fails(late_worker: Files):
    """Новая папка: воркер успел создать в ней файл, пока backend её убирал, и удалить
    папку с первого раза не вышло. Она убирается следующей проверкой."""
    files = late_worker
    files.rig.worker.sftp_start_after_loss = False
    files.rig.worker.sftp_start_delay = 0.0
    files.rig.worker.sftp_delay = 0.2
    make_tree(files.local / "big", {"a.bin": os.urandom(4 * MB)})
    refused: list[str] = []

    def refuse(action: str, payload: dict) -> str | None:
        if action == "sftp_remove_dir" and not refused:
            refused.append(str(payload.get("remote_path")))
            return "sftp_failed: Failed to remove remote directory: Failure"
        return None

    files.rig.worker.sftp_fail_if = refuse
    started = await files.rig.api.post(
        f"{files.base}/transfers",
        json={"direction": "upload", "local_path": str(files.local / "big"), "remote_path": "~"},
    )
    transfer_id = started.json()["id"]
    async with asyncio.timeout(10):
        while not list(files.server("~").glob(".synex-*/big/a.bin")):
            await asyncio.sleep(0.02)
    await files.rig.api.delete(f"/api/sftp/transfers/{transfer_id}")
    assert (await files.finished(transfer_id))["status"] == "cancelled"
    assert refused  # первая уборка не удалась
    async with asyncio.timeout(5):
        while list(files.server("~").iterdir()):
            await asyncio.sleep(0.02)


async def test_a_folder_larger_than_the_limit(files: Files, monkeypatch):
    monkeypatch.setattr(settings, "SFTP_FOLDER_LIMIT", 4)
    make_tree(files.local / "many", {f"d/{i}.txt": b"x" for i in range(6)})
    make_tree(files.server("~/many-here"), {f"d/{i}.txt": b"x" for i in range(6)})
    up = await files.transfer(
        direction="upload", local_path=str(files.local / "many"), remote_path="~"
    )
    assert (up["status"], up["error"]) == ("failed", "sftp_folder_too_large"), up
    assert "SFTP_FOLDER_LIMIT" in up["detail"]
    down = await files.transfer(
        direction="download", local_path=str(files.local), remote_path="many-here"
    )
    assert (down["status"], down["error"]) == ("failed", "sftp_folder_too_large"), down
    assert [p.name for p in files.server("~").iterdir()] == ["many-here"]
    assert [p.name for p in files.local.iterdir()] == ["many"]


# ------------------------------------------------------------------------ merge

LOCAL_SITE: dict[str, bytes | None] = {
    "index.html": b"<h1>new</h1>",
    "css/site.css": b"body { color: black }",
    "js/app.js": b"console.log('new')",
    "img": None,
}
SERVER_SITE: dict[str, bytes | None] = {
    "index.html": b"<h1>old</h1>",
    "css/site.css": b"old css",
    "css/print.css": b"print css",  # в источнике нет: должен остаться
    "uploads/photo.jpg": b"photo",
}
MERGED = with_parents({**SERVER_SITE, **LOCAL_SITE})


async def test_upload_merges_into_an_existing_folder(files: Files):
    make_tree(files.local / "site", LOCAL_SITE)
    make_tree(files.server("~/site"), SERVER_SITE)
    body = {"direction": "upload", "local_path": str(files.local / "site"), "remote_path": "~"}
    refused = await files.rig.api.post(f"{files.base}/transfers", json=body)
    assert (refused.status_code, refused.json()["error"]) == (409, "sftp_target_exists")
    assert "overwrite=true to merge" in refused.json()["detail"]

    done = await files.transfer(**body, overwrite=True)
    assert (done["status"], done["verified"]) == ("completed", True), done
    assert (done["files_done"], done["files_total"], done["skipped_count"]) == (3, 3, 0)
    assert snapshot(files.server("~/site")) == MERGED
    assert [p.name for p in files.server("~").iterdir()] == ["site"]


async def test_download_merges_into_an_existing_folder(files: Files):
    make_tree(files.server("~/site"), LOCAL_SITE)  # «новое» теперь на сервере
    make_tree(files.local / "site", SERVER_SITE)
    body = {"direction": "download", "local_path": str(files.local), "remote_path": "site"}
    refused = await files.rig.api.post(f"{files.base}/transfers", json=body)
    assert (refused.status_code, refused.json()["error"]) == (409, "sftp_local_exists")

    done = await files.transfer(**body, overwrite=True)
    assert (done["status"], done["verified"]) == ("completed", True), done
    assert (done["files_done"], done["files_total"]) == (3, 3)
    assert snapshot(files.local / "site") == MERGED
    assert temporary(files.local) == []


async def test_merge_replaces_a_file_only_after_it_is_checked(files: Files):
    """При объединении файл идёт под временным именем: пока он не передан и не сверен,
    прежний файл на месте. После сбоя переданное раньше остаётся, временного нет."""
    make_tree(files.local / "site", {"a.txt": b"new a", "b.txt": b"new b", "c.txt": b"new c"})
    make_tree(files.server("~/site"), {"a.txt": b"old a", "b.txt": b"old b", "c.txt": b"old c"})
    seen: list[dict[str, bytes | None]] = []

    def refuse(action: str, payload: dict) -> str | None:
        if action != "sftp_upload_file":
            return None
        seen.append(snapshot(files.server("~/site")))
        if len(seen) == 2:  # второй файл не уходит
            return "SSH internal error: Failed to upload file: Failure"
        return None

    files.rig.worker.sftp_fail_if = refuse
    info = await files.transfer(
        direction="upload",
        local_path=str(files.local / "site"),
        remote_path="~",
        overwrite=True,
    )
    assert (info["status"], info["error"]) == ("failed", "sftp_failed"), info
    # К началу второй загрузки первый файл передан, но ещё не на месте
    assert {k: v for k, v in seen[1].items() if ".synex-" not in k} == {
        "a.txt": b"old a",
        "b.txt": b"old b",
        "c.txt": b"old c",
    }
    assert snapshot(files.server("~/site")) == {
        "a.txt": b"old a",
        "b.txt": b"old b",
        "c.txt": b"old c",
    }


async def test_old_file_comes_back_if_the_new_one_cannot_be_put_in_place(files: Files):
    make_tree(files.local / "site", {"a.txt": b"new a", "b.txt": b"new b"})
    make_tree(files.server("~/site"), {"a.txt": b"old a", "b.txt": b"old b"})

    def refuse(action: str, payload: dict) -> str | None:
        target = str(payload.get("destination_path") or "")
        source = str(payload.get("remote_path") or "")
        if action == "sftp_move_file" and target.endswith("/a.txt") and ".previous" not in source:
            return "sftp_permission_denied: Failed to move remote file: Permission denied"
        return None

    files.rig.worker.sftp_fail_if = refuse
    info = await files.transfer(
        direction="upload",
        local_path=str(files.local / "site"),
        remote_path="~",
        overwrite=True,
    )
    assert (info["status"], info["files_done"], info["files_total"]) == ("completed", 1, 1), info
    assert [s["path"] for s in info["skipped"]] == ["a.txt"]
    assert "No permission" in info["skipped"][0]["reason"]
    assert snapshot(files.server("~/site")) == {"a.txt": b"old a", "b.txt": b"new b"}


async def test_old_file_comes_back_when_the_transfer_session_is_lost(files: Files):
    """Служебная сессия пропала между двумя переименованиями (отмена, обрыв): прежний
    файл уже отошёл в сторону, а новый на его место не встал. Прежний возвращается через
    основную сессию, под временным именем ничего не остаётся."""
    make_tree(files.local / "site", {"a.txt": b"new a"})
    make_tree(files.server("~/site"), {"a.txt": b"old a"})
    lost = False

    def refuse(action: str, payload: dict) -> str | None:
        nonlocal lost
        if action != "sftp_move_file":
            return None
        target = str(payload.get("destination_path") or "")
        source = str(payload.get("remote_path") or "")
        if target.endswith("/a.txt") and ".previous" not in source:
            lost = True
        if lost and payload.get("session_id") != files.rig.session_id:
            return "SSH internal error: Failed to move remote file: Connection lost"
        return None

    files.rig.worker.sftp_fail_if = refuse
    info = await files.transfer(
        direction="upload",
        local_path=str(files.local / "site"),
        remote_path="~",
        overwrite=True,
    )
    assert (info["status"], info["error"]) == ("failed", "sftp_failed"), info
    assert lost
    assert snapshot(files.server("~/site")) == {"a.txt": b"old a"}


async def test_a_replaced_file_that_cannot_be_removed_does_not_fail_the_file(files: Files):
    """Новый файл уже на месте, а прежний (под временным именем) сервер удалить не дал:
    файл считается переданным."""
    make_tree(files.local / "site", {"a.txt": b"new a", "b.txt": b"new b"})
    make_tree(files.server("~/site"), {"a.txt": b"old a"})

    def refuse(action: str, payload: dict) -> str | None:
        if action == "sftp_remove_file" and ".previous" in str(payload.get("remote_path")):
            return "sftp_permission_denied: Failed to remove remote file: Permission denied"
        return None

    files.rig.worker.sftp_fail_if = refuse
    info = await files.transfer(
        direction="upload",
        local_path=str(files.local / "site"),
        remote_path="~",
        overwrite=True,
    )
    assert (info["status"], info["files_done"], info["skipped"]) == ("completed", 2, []), info
    kept = {k: v for k, v in snapshot(files.server("~/site")).items() if ".previous" not in k}
    assert kept == {"a.txt": b"new a", "b.txt": b"new b"}


# ---------------------------------------------------------------------- skipped


async def test_what_is_in_the_way_is_skipped_when_merging(files: Files):
    make_tree(
        files.local / "site",
        {"logs": b"a file here", "data/a.txt": b"a", "data/b/c.txt": b"c", "ok.txt": b"ok"},
    )
    # На сервере logs это папка, а data/b это файл
    make_tree(files.server("~/site"), {"logs/app.log": b"log", "data/b": b"file there"})
    done = await files.transfer(
        direction="upload",
        local_path=str(files.local / "site"),
        remote_path="~",
        overwrite=True,
    )
    assert (done["status"], done["files_done"], done["files_total"]) == ("completed", 2, 2), done
    assert done["skipped"] == [
        {"path": "logs", "reason": folders.REASON_DIR_ON_SERVER},
        {"path": "data/b/", "reason": folders.REASON_FILE_ON_SERVER},
    ]
    assert done["skipped_count"] == 2
    assert (done["bytes_done"], done["bytes_total"]) == (3, 3)  # a.txt и ok.txt
    assert snapshot(files.server("~/site")) == {
        "data": None,
        "data/a.txt": b"a",
        "data/b": b"file there",
        "logs": None,
        "logs/app.log": b"log",
        "ok.txt": b"ok",
    }

    # Обратно: здесь ok.txt папка, а data файл
    mirror = files.local / "mirror"
    make_tree(mirror / "site", {"logs/x": b"here", "ok.txt/x": b"dir here", "data": b"file"})
    back = await files.transfer(
        direction="download", local_path=str(mirror), remote_path="~/site", overwrite=True
    )
    assert (back["status"], back["files_done"], back["files_total"]) == ("completed", 1, 1), back
    assert back["skipped"] == [
        {"path": "ok.txt", "reason": folders.REASON_DIR_HERE},
        {"path": "data/", "reason": folders.REASON_FILE_HERE},
    ]
    assert snapshot(mirror / "site") == {
        "data": b"file",
        "logs": None,
        "logs/app.log": b"log",  # logs папка там и там: объединены
        "logs/x": b"here",
        "ok.txt": None,
        "ok.txt/x": b"dir here",
    }


@pytest.mark.skipif(os.name == "nt", reason="symlinks need developer mode on Windows")
async def test_links_are_not_followed(files: Files):
    make_tree(files.local / "site", {"a.txt": b"a", "real/b.txt": b"b"})
    (files.local / "site" / "to-file").symlink_to("a.txt")
    (files.local / "site" / "loop").symlink_to(files.local / "site")
    up = await files.transfer(
        direction="upload", local_path=str(files.local / "site"), remote_path="~"
    )
    assert (up["status"], up["files_done"]) == ("completed", 2), up
    assert up["skipped"] == [
        {"path": "loop", "reason": "Links are not transferred"},
        {"path": "to-file", "reason": "Links are not transferred"},
    ]
    assert snapshot(files.server("~/site")) == with_parents({"a.txt": b"a", "real/b.txt": b"b"})

    files.server("~/site/back").symlink_to(files.server("~"))
    down = await files.transfer(
        direction="download", local_path=str(files.local / "copy"), remote_path="~/site"
    )
    assert (down["status"], down["files_done"]) == ("completed", 2), down
    assert down["skipped"] == [{"path": "back", "reason": "Links are not transferred"}]
    assert snapshot(files.local / "copy") == with_parents({"a.txt": b"a", "real/b.txt": b"b"})


def case_sensitive(directory: Path) -> bool:
    """Различает ли файловая система directory имена, которые отличаются только регистром
    (Linux да; macOS и Windows по умолчанию нет)."""
    probe = directory / "Synex-Case-Probe"
    probe.write_bytes(b"")
    try:
        return not (directory / "synex-case-probe").exists()
    finally:
        probe.unlink()


@not_windows
async def test_names_this_computer_cannot_use_are_skipped(files: Files, monkeypatch):
    """Как при скачивании на Windows: имена с двоеточием и имена, которые различаются
    только регистром букв, там не сохранить. Остальная папка скачивается."""
    windows_rules = local.name_problem
    monkeypatch.setattr(local, "name_problem", lambda name: windows_rules(name, windows=True))
    windows_keys = local.name_key
    monkeypatch.setattr(local, "name_key", lambda name: windows_keys(name, platform="win32"))
    content: dict[str, bytes | None] = {
        "app.log": b"app",
        "2026-11-16T10:00.log": b"colon",
        "Readme": b"first",
        "con/x.txt": b"reserved",
        "sub/ok.txt": b"ok",
    }
    files.server("~/logs").mkdir(parents=True)
    # Файлы фейкового сервера лежат на диске этой машины: «Readme» и «readme» рядом
    # бывают только там, где регистр различается (на macOS это один файл)
    two_cases = case_sensitive(files.server("~/logs"))
    if two_cases:
        content["readme"] = b"second"
    make_tree(files.server("~/logs"), content)
    done = await files.transfer(
        direction="download", local_path=str(files.local), remote_path="logs"
    )
    assert (done["status"], done["files_done"], done["files_total"]) == ("completed", 3, 3), done
    reasons = {s["path"]: s["reason"] for s in done["skipped"]}
    assert set(reasons) == {"2026-11-16T10:00.log", "con/"} | ({"readme"} if two_cases else set())
    assert "Windows does not allow" in reasons["2026-11-16T10:00.log"]
    if two_cases:
        assert reasons["readme"] == folders.REASON_CASE
    assert "reserved by Windows" in reasons["con/"]
    assert snapshot(files.local / "logs") == with_parents(
        {"app.log": b"app", "Readme": b"first", "sub/ok.txt": b"ok"}
    )


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs file permissions")
async def test_unreadable_files_are_skipped(files: Files):
    make_tree(files.local / "site", {"a.txt": b"a", "secret.txt": b"s", "closed/x.txt": b"x"})
    os.chmod(files.local / "site" / "secret.txt", 0)
    os.chmod(files.local / "site" / "closed", 0)
    try:
        done = await files.transfer(
            direction="upload", local_path=str(files.local / "site"), remote_path="~"
        )
    finally:
        os.chmod(files.local / "site" / "closed", 0o755)
    assert (done["status"], done["files_done"]) == ("completed", 1), done
    assert done["skipped"] == [
        {"path": "closed/", "reason": "No permission to read"},
        {"path": "secret.txt", "reason": "No permission to read"},
    ]
    assert snapshot(files.server("~/site")) == {"a.txt": b"a"}


async def test_refused_by_the_server_is_skipped(files: Files):
    """Файл, который сервер не отдаёт или не принимает из-за прав, пропускается; сбой
    без такой причины останавливает передачу (см. тест выше)."""
    make_tree(files.server("~/logs"), {"app.log": b"app", "btmp": b"root only", "z.log": b"z"})

    def refuse(action: str, payload: dict) -> str | None:
        if action == "sftp_download_file" and str(payload.get("remote_path")).endswith("/btmp"):
            if files.rig.worker.sftp_build == "2026-11-12":
                return "SSH internal error: Failed to open remote file: Permission denied"
            return "sftp_permission_denied: Failed to open remote file: Permission denied"
        return None

    files.rig.worker.sftp_fail_if = refuse
    done = await files.transfer(
        direction="download", local_path=str(files.local), remote_path="logs"
    )
    assert (done["status"], done["files_done"], done["files_total"]) == ("completed", 2, 2), done
    assert [s["path"] for s in done["skipped"]] == ["btmp"]
    assert "No permission on the server" in done["skipped"][0]["reason"]
    assert (done["bytes_done"], done["bytes_total"], done["percent"]) == (4, 4, 100.0)
    assert snapshot(files.local / "logs") == {"app.log": b"app", "z.log": b"z"}


async def test_only_the_first_skipped_are_listed(files: Files, monkeypatch):
    monkeypatch.setattr(folders, "SKIPPED_SHOWN", 3)
    monkeypatch.setattr(local, "name_problem", lambda name: "x" if name != "ok" else None)
    make_tree(files.server("~/many"), {**{f"bad{i}": b"x" for i in range(7)}, "ok": b"ok"})
    done = await files.transfer(
        direction="download", local_path=str(files.local / "many"), remote_path="many"
    )
    assert (done["status"], done["files_done"]) == ("completed", 1), done
    assert (len(done["skipped"]), done["skipped_count"]) == (3, 7)


# ----------------------------------------------------------------- verification


async def test_checksums_are_counted_in_batches(files: Files, monkeypatch):
    """Одна команда sha256 на пачку файлов, а не на каждый файл."""
    content = {f"a/{i:02}.txt": f"file {i}".encode() for i in range(10)}
    content.update({f"b/{i:02}.txt": f"other {i}".encode() for i in range(10)})
    make_tree(files.local / "many", content)
    monkeypatch.setattr(folders, "BATCH_FILES", 4)
    done = await files.transfer(
        direction="upload", local_path=str(files.local / "many"), remote_path="~"
    )
    assert (done["status"], done["verified"], done["files_done"]) == ("completed", True, 20), done
    commands = [
        r["payload"]["command"]
        for r in files.requests("ssh_exec")
        if "sha256sum" in r["payload"]["command"]
    ]
    assert len(commands) == 6  # в каждой папке пачки по 4, 4 и 2 файла
    assert snapshot(files.server("~/many")) == with_parents(content)


async def test_a_file_that_does_not_match_is_sent_again(files: Files):
    make_tree(files.local / "site", {"a.txt": b"a", "live.log": b"log line\n", "z.txt": b"z"})
    real = files.service.remote.checksums
    asked: list[list[str]] = []

    async def first_time_wrong(
        session_id: str, paths: list[str], companion: object = None
    ) -> list[str | None] | None:
        asked.append(paths)
        sums = await real(session_id, paths, companion)  # type: ignore[arg-type]
        assert sums is not None
        if len(asked) == 1:  # как будто live.log дописали, пока он передавался
            sums[1] = "0" * 64
        return sums

    files.service.remote.checksums = first_time_wrong  # type: ignore[method-assign]
    done = await files.transfer(
        direction="upload", local_path=str(files.local / "site"), remote_path="~"
    )
    assert (done["status"], done["files_done"], done["skipped_count"]) == ("completed", 3, 0)
    assert done["verified"] is True
    assert [len(paths) for paths in asked] == [3, 1]  # вся пачка, потом один файл заново
    sent = [r["payload"]["local_path"] for r in files.requests("sftp_upload_file")]
    assert sent.count(str(files.local / "site" / "live.log")) == 2
    assert snapshot(files.server("~/site"))["live.log"] == b"log line\n"


async def test_a_file_that_never_matches_is_skipped(files: Files):
    make_tree(files.server("~/logs"), {"a.log": b"a", "live.log": b"grows", "z.log": b"z"})
    real = files.service.remote.checksums

    async def live_log_differs(
        session_id: str, paths: list[str], companion: object = None
    ) -> list[str | None] | None:
        sums = await real(session_id, paths, companion)  # type: ignore[arg-type]
        assert sums is not None
        return ["0" * 64 if path.endswith("live.log") else value
                for path, value in zip(paths, sums, strict=True)]  # fmt: skip

    files.service.remote.checksums = live_log_differs  # type: ignore[method-assign]
    done = await files.transfer(
        direction="download", local_path=str(files.local), remote_path="logs"
    )
    assert (done["status"], done["files_done"], done["files_total"]) == ("completed", 2, 2), done
    assert done["skipped"] == [{"path": "live.log", "reason": folders.REASON_CHANGED}]
    assert snapshot(files.local / "logs") == {"a.log": b"a", "z.log": b"z"}


async def test_without_sha256_on_the_server_sizes_are_checked(files: Files):
    files.rig.worker.shell.programs.pop("sha256sum")  # type: ignore[union-attr]
    make_tree(files.local / "site", {"a.txt": b"a", "sub/b.txt": b"bb"})
    done = await files.transfer(
        direction="upload", local_path=str(files.local / "site"), remote_path="~"
    )
    assert (done["status"], done["verified"], done["files_done"]) == ("completed", None, 2), done
    assert (await files.rig.api.get(f"{files.base}/info")).json()["checksum"] is False
    assert snapshot(files.server("~/site")) == with_parents({"a.txt": b"a", "sub/b.txt": b"bb"})

    # Размер не сошёлся и со второй попытки: файл не сохранён
    real = files.service.remote.size_of

    async def wrong_size(session_id: str, path: str, companion: object) -> int | None:
        size = await real(session_id, path, companion)  # type: ignore[arg-type]
        return size + 1 if size is not None and path.endswith("b.txt") else size

    files.service.remote.size_of = wrong_size  # type: ignore[method-assign]
    again = await files.transfer(
        direction="download", local_path=str(files.local / "copy"), remote_path="site"
    )
    assert (again["status"], again["files_done"]) == ("completed", 1), again
    assert again["skipped"] == [{"path": "sub/b.txt", "reason": folders.REASON_CHANGED}]
    assert snapshot(files.local / "copy") == {"a.txt": b"a", "sub": None}


async def test_terminal_works_during_a_folder_transfer(files: Files):
    files.rig.worker.sftp_delay = 0.2
    make_tree(files.local / "big", {"a.bin": os.urandom(3 * MB), "b.bin": os.urandom(MB)})
    started = await files.rig.api.post(
        f"{files.base}/transfers",
        json={"direction": "upload", "local_path": str(files.local / "big"), "remote_path": "~"},
    )
    transfer_id = started.json()["id"]
    async with asyncio.timeout(10):
        while (await files.rig.api.get(f"/api/sftp/transfers/{transfer_id}")).json()[
            "status"
        ] != "running":
            await asyncio.sleep(0.02)
    executed = await files.rig.api.post(
        f"/api/ssh/sessions/{files.rig.session_id}/exec", json={"command": "echo free"}
    )
    assert (executed.status_code, executed.json()["stdout"]) == (200, "free")
    running = (await files.rig.api.get(f"/api/sftp/transfers/{transfer_id}")).json()
    assert running["status"] == "running"  # команда не ждала конца передачи
    assert (await files.finished(transfer_id))["status"] == "completed"


async def test_folders_the_server_does_not_open_are_skipped(files: Files):
    """Папка сервера, которую нельзя прочитать (или в которую нельзя писать), пропускается
    со всем содержимым; остальное передаётся."""
    old = files.rig.worker.sftp_build == "2026-11-12"
    denied = (
        "SSH internal error: Failed to read remote directory: Permission denied"
        if old
        else "sftp_permission_denied: Failed to read remote directory: Permission denied"
    )
    make_tree(files.server("~/var"), {"log/app.log": b"app", "private/key.pem": b"key"})

    def refuse(action: str, payload: dict) -> str | None:
        path = str(payload.get("remote_path"))
        if action in ("sftp_list_dir", "sftp_create_dir") and path.endswith("/private"):
            return denied
        return None

    files.rig.worker.sftp_fail_if = refuse
    down = await files.transfer(
        direction="download", local_path=str(files.local), remote_path="var"
    )
    assert (down["status"], down["files_done"], down["files_total"]) == ("completed", 1, 1), down
    assert down["skipped"] == [{"path": "private/", "reason": "No permission to read"}]
    assert snapshot(files.local / "var") == with_parents({"log/app.log": b"app"})

    make_tree(files.local / "up", {"a.txt": b"a", "private/b.txt": b"bb", "private/c/d.txt": b"d"})
    up = await files.transfer(
        direction="upload", local_path=str(files.local / "up"), remote_path="~"
    )
    assert (up["status"], up["files_done"], up["files_total"]) == ("completed", 1, 1), up
    assert (up["bytes_done"], up["bytes_total"]) == (1, 1)
    assert [s["path"] for s in up["skipped"]] == ["private/"]
    assert "No permission on the server" in up["skipped"][0]["reason"]
    assert snapshot(files.server("~/up")) == {"a.txt": b"a"}


async def test_a_folder_goes_in_the_main_session_without_a_second_connection(files: Files):
    """Воркер не дал второе подключение: папка идёт в основной сессии."""

    async def no_companion(session_id: str, purpose: str) -> None:
        return None

    files.rig.manager.open_companion = no_companion  # type: ignore[method-assign]
    make_tree(files.local / "site", {"a.txt": b"a", "sub/b.txt": b"b"})
    make_tree(files.server("~/site"), {"old.txt": b"old"})
    up = await files.transfer(
        direction="upload",
        local_path=str(files.local / "site"),
        remote_path="~",
        overwrite=True,
    )
    assert (up["status"], up["files_done"], up["verified"]) == ("completed", 2, True), up
    assert {r["id"] for r in files.requests("sftp_upload_file")} == {files.rig.session_id}
    down = await files.transfer(
        direction="download", local_path=str(files.local / "copy"), remote_path="site"
    )
    assert (down["status"], down["files_done"]) == ("completed", 3), down
    assert snapshot(files.local / "copy") == with_parents(
        {"a.txt": b"a", "old.txt": b"old", "sub/b.txt": b"b"}
    )


async def test_the_downloaded_folder_waits_for_windows_to_release_it(files: Files, monkeypatch):
    """На Windows папку не переименовать, пока файл в ней открыт другой программой
    (антивирус): backend пробует несколько раз. Не вышло: временная папка убирается."""
    monkeypatch.setattr(folders, "RENAME_PAUSE", 0.01)
    make_tree(files.server("~/site"), {"a.txt": b"a"})
    real = local.replace_dir
    calls: list[Path] = []

    def busy_twice(part: Path, target: Path) -> None:
        calls.append(part)
        if len(calls) < 3:
            raise local.SftpLocalPermissionError(f"Cannot save {target}: a file in it is open")
        real(part, target)

    monkeypatch.setattr(local, "replace_dir", busy_twice)
    done = await files.transfer(
        direction="download", local_path=str(files.local), remote_path="site"
    )
    assert (done["status"], len(calls)) == ("completed", 3), done
    assert snapshot(files.local) == {"site": None, "site/a.txt": b"a"}

    def always_busy(part: Path, target: Path) -> None:
        raise local.SftpLocalPermissionError(f"Cannot save {target}: a file in it is open")

    monkeypatch.setattr(local, "replace_dir", always_busy)
    failed = await files.transfer(
        direction="download", local_path=str(files.local / "second"), remote_path="site"
    )
    assert (failed["status"], failed["error"]) == ("failed", "sftp_local_permission_denied")
    assert snapshot(files.local) == {"site": None, "site/a.txt": b"a"}


async def test_temporary_files_windows_keeps_busy_are_removed_later(files: Files, monkeypatch):
    """Файл отменённой передачи воркер может держать открытым ещё какое-то время: на
    Windows его в этот момент не удалить. Уборка повторяется в фоне."""
    from app.plugins.sftp import transfers

    monkeypatch.setattr(transfers, "CLEANUP_PAUSE", 0.02)
    make_tree(files.server("~/site"), {"a.txt": b"a"})
    real = local.remove_tree
    attempts: list[Path] = []

    def busy_at_first(directory: Path) -> bool:
        attempts.append(directory)
        return len(attempts) > 2 and real(directory)

    monkeypatch.setattr(local, "remove_tree", busy_at_first)

    def refuse(action: str, payload: dict) -> str | None:
        return (
            "SSH internal error: Failed to download file: Failure" if "download" in action else None
        )

    files.rig.worker.sftp_fail_if = refuse
    failed = await files.transfer(
        direction="download", local_path=str(files.local), remote_path="site"
    )
    assert (failed["status"], failed["error"]) == ("failed", "sftp_failed"), failed
    async with asyncio.timeout(5):
        while list(files.local.iterdir()):
            await asyncio.sleep(0.02)
    assert len(attempts) == 3


async def test_checksums_of_several_files(files: Files):
    import hashlib

    files.put("~/a.txt", b"a")
    files.put("~/with space & 'quotes'.txt", b"b")
    sid = files.rig.session_id
    remote = files.service.remote
    paths = [f"{HOME}/a.txt", f"{HOME}/missing.txt", f"{HOME}/with space & 'quotes'.txt"]
    assert await remote.checksums(sid, paths) == [
        hashlib.sha256(b"a").hexdigest(),
        None,  # с этим файлом не вышло, остальные посчитаны
        hashlib.sha256(b"b").hexdigest(),
    ]
    asked = [r["payload"]["command"] for r in files.requests("ssh_exec")]
    assert len([command for command in asked if "sha256sum" in command]) == 1
    assert (await remote.info(sid)).checksum is True
    assert await remote.checksums(sid, []) == []

    # Нечем считать: None вместо списка, и больше сервер об этом не спрашивают
    remote.forget(sid)
    files.rig.worker.shell.programs.pop("sha256sum")  # type: ignore[union-attr]
    assert await remote.checksums(sid, paths[:1]) is None
    commands = len(files.requests("ssh_exec"))
    assert await remote.checksums(sid, paths) is None
    assert len(files.requests("ssh_exec")) == commands


def test_checksum_lines_and_batches():
    from app.plugins.sftp.remote import CHECKSUM_BATCH, CHECKSUM_COMMAND_CHARS, _batches, _sum_of

    one, two = "a" * 64, "B" * 64
    assert _sum_of(f"{one}  /srv/file.txt") == one  # sha256sum, shasum
    assert _sum_of(f"\\{one}  /srv/odd\\\\name") == one  # sha256sum: в имени спецсимволы
    assert _sum_of(f"{one} */srv/file.txt") == one  # openssl dgst -r
    assert _sum_of(f"SHA256(/srv/file.txt)= {two}") == two.lower()  # старый openssl без -r
    # Имя файла само похоже на сумму
    assert _sum_of(f"{one}  /srv/{two}") == one
    assert _sum_of(f"SHA256(/srv/{one})= {two}") == two.lower()
    assert _sum_of("-") is None and _sum_of("sha256sum: /x: Permission denied") is None

    short = [f"/srv/{i}.txt" for i in range(CHECKSUM_BATCH * 2 + 5)]
    assert [len(batch) for batch in _batches(short)] == [CHECKSUM_BATCH, CHECKSUM_BATCH, 5]
    assert [path for batch in _batches(short) for path in batch] == short
    # Длинные пути: пачку ограничивает длина команды (путь входит в неё три раза)
    long = ["/srv/" + "x" * 1500 + str(i) for i in range(5)]
    assert 3 * 1500 * 2 < CHECKSUM_COMMAND_CHARS < 3 * 1500 * 3
    assert [len(batch) for batch in _batches(long)] == [2, 2, 1]
    assert list(_batches([])) == []


async def test_a_busy_terminal_does_not_hold_a_folder_transfer(files: Files):
    """Основная сессия занята долгой командой: начатая передача папки идёт и сверяется
    без неё (всё, кроме отметок прогресса, делает служебная сессия)."""
    files.rig.worker.sftp_delay = 0.1
    content = {"a.bin": os.urandom(2 * MB), "sub/b.txt": b"b"}
    make_tree(files.local / "site", content)
    make_tree(files.server("~/site"), {"a.bin": b"old"})
    make_tree(files.server("~/logs"), content)
    sid = files.rig.session_id
    bodies = [
        {"direction": "upload", "local_path": str(files.local / "site"), "remote_path": "~",
         "overwrite": True},  # объединение
        {"direction": "download", "local_path": str(files.local), "remote_path": "logs"},
    ]  # fmt: skip
    for body in bodies:
        started = await files.rig.api.post(f"{files.base}/transfers", json=body)
        assert started.status_code == 202, started.text
        busy = asyncio.ensure_future(
            files.rig.manager.request(sid, "test_sleep", {"seconds": 1.5}, timeout=10)
        )
        done = await files.finished(started.json()["id"])
        assert (done["status"], done["files_done"], done["verified"]) == ("completed", 2, True)
        assert not busy.done()  # команда в основной сессии всё это время шла
        busy.cancel()
        await asyncio.gather(busy, return_exceptions=True)
    assert snapshot(files.server("~/site")) == with_parents(content)
    assert snapshot(files.local / "logs") == with_parents(content)
