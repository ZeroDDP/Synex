"""Плагин sftp через HTTP API. Сервер здесь фейковый воркер (tests/fake_worker.py,
FakeSftp) с эмулятором shell, поэтому тесты идут на Windows, Linux и macOS. С настоящим
воркером и sshd то же проверяет tests/integration/test_sftp_cycle.py."""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import quote

import pytest

from app.plugins.sftp import service
from app.plugins.sftp.routes import TRANSFER_ID_HEADER
from tests.conftest import needs_unicode_file_names
from tests.fake_shell import HOME
from tests.sftp_rig import Files, files_rig

MB = 1024 * 1024


# Сборки воркера: 2026-11-14 (коды ошибок, sftp_stat, mtime) и 2026-11-12 (только
# тексты ошибок): backend работает с обеими, каждый тест идёт с каждой
@pytest.fixture(params=["2026-11-14", "2026-11-12"])
async def files(request, tmp_path: Path, missing_socket_path, monkeypatch) -> AsyncIterator[Files]:
    async with files_rig(tmp_path, missing_socket_path, monkeypatch, request.param) as rig:
        yield rig


# ------------------------------------------------------------------- server


async def test_info(files: Files):
    info = (await files.rig.api.get(f"{files.base}/info")).json()
    assert info == {"engine": "core", "home": HOME, "user": "root", "checksum": None}
    # Домашняя папка запоминается: второй раз pwd не нужен
    calls = len(files.requests("ssh_exec"))
    await files.rig.api.get(f"{files.base}/info")
    assert len(files.requests("ssh_exec")) == calls


async def test_list_home_sorts_and_describes_entries(files: Files):
    files.put("~/b.txt", b"12345")
    files.put("~/A.txt", b"")
    files.put("~/.env", b"x")
    files.server("~/docs/sub").mkdir(parents=True)
    os.chmod(files.server("~/b.txt"), 0o640)

    listing = (await files.rig.api.get(f"{files.base}/list")).json()
    assert (listing["path"], listing["parent"], listing["home"]) == (HOME, "/home", HOME)
    assert [(e["name"], e["type"], e["is_dir"]) for e in listing["entries"]] == [
        ("docs", "dir", True),
        (".env", "file", False),
        ("A.txt", "file", False),
        ("b.txt", "file", False),
    ]
    file = listing["entries"][3]
    assert (file["path"], file["size"], file["hidden"]) == (f"{HOME}/b.txt", 5, False)
    if os.name != "nt":  # права файлов на Windows не настоящие
        assert (file["permissions"], file["mode"]) == ("-rw-r-----", 0o640)
    assert listing["entries"][0]["permissions"].startswith("d")
    assert listing["entries"][1]["hidden"] is True

    docs = (await files.rig.api.get(f"{files.base}/list", params={"path": "~/docs"})).json()
    assert (docs["path"], [e["name"] for e in docs["entries"]]) == (f"{HOME}/docs", ["sub"])
    root = (await files.rig.api.get(f"{files.base}/list", params={"path": "/"})).json()
    assert (root["path"], root["parent"]) == ("/", None)
    assert {"home", "tmp"} <= {e["name"] for e in root["entries"]}


@pytest.mark.skipif(os.name == "nt", reason="symlinks need developer mode on Windows")
async def test_links_to_directories_open_like_directories(files: Files):
    files.server("~/real").mkdir()
    files.put("~/file.txt")
    files.server("~/to-dir").symlink_to(files.server("~/real"))
    files.server("~/to-file").symlink_to(files.server("~/file.txt"))

    entries = {
        e["name"]: e for e in (await files.rig.api.get(f"{files.base}/list")).json()["entries"]
    }
    assert (entries["to-dir"]["type"], entries["to-dir"]["target_type"]) == ("link", "dir")
    assert entries["to-dir"]["is_dir"] is True
    assert (entries["to-file"]["target_type"], entries["to-file"]["is_dir"]) == (None, False)
    removed = await files.rig.api.delete(f"{files.base}/entry", params={"path": "to-dir"})
    assert removed.status_code == 200, removed.text
    assert files.server("~/real").is_dir()  # удалена ссылка, а не папка


@needs_unicode_file_names
async def test_names_with_spaces_quotes_and_cyrillic(files: Files):
    # Файлы фейкового сервера лежат на диске этой машины, а Windows не даёт имени '"'
    name = "отчёт 'май' (v2).txt" if os.name == "nt" else "отчёт 'май' \"v2\".txt"
    files.put(f"~/{name}", b"ru")
    entries = (await files.rig.api.get(f"{files.base}/list")).json()["entries"]
    assert [e["name"] for e in entries] == [name]
    moved = await files.rig.api.post(
        f"{files.base}/move", json={"source": name, "target": f"~/{name}.bak"}
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["name"] == f"{name}.bak"


async def test_stat(files: Files):
    files.put("~/a.txt", b"abc")
    stat = await files.rig.api.get(f"{files.base}/stat", params={"path": "~/a.txt"})
    assert stat.status_code == 200, stat.text
    assert (stat.json()["path"], stat.json()["size"], stat.json()["type"]) == (
        f"{HOME}/a.txt",
        3,
        "file",
    )
    root = (await files.rig.api.get(f"{files.base}/stat", params={"path": "/"})).json()
    assert (root["path"], root["type"]) == ("/", "dir")


async def test_details_of_the_new_worker(files: Files):
    """Сборка 2026-11-14: время изменения, владелец и цель ссылки в записях, сведения об
    одном пути через sftp_stat (без списка всей папки). Со старой сборкой этих полей нет."""
    new = files.rig.worker.sftp_build == "2026-11-14"
    file = files.put("~/a.txt", b"abc")
    os.utime(file, (1_700_000_000, 1_700_000_000))
    listed = {
        e["name"]: e for e in (await files.rig.api.get(f"{files.base}/list")).json()["entries"]
    }
    stat = (await files.rig.api.get(f"{files.base}/stat", params={"path": "~/a.txt"})).json()
    for entry in (listed["a.txt"], stat):
        if new:
            assert entry["modified_at"].startswith("2023-11-14T22:13:20")
            assert entry["uid"] == getattr(file.stat(), "st_uid", None)
        else:
            assert (entry["modified_at"], entry["uid"], entry["gid"]) == (None, None, None)
    lists_before = len(files.requests("sftp_list_dir"))
    await files.rig.api.get(f"{files.base}/stat", params={"path": "~/a.txt"})
    if new:
        assert len(files.requests("sftp_list_dir")) == lists_before  # хватило sftp_stat
        assert files.requests("sftp_stat")[-1]["payload"]["remote_path"] == f"{HOME}/a.txt"
    else:
        assert len(files.requests("sftp_list_dir")) == lists_before + 1

    if os.name != "nt":  # ссылки на Windows нужны права разработчика
        files.server("~/link").symlink_to("a.txt")
        link = (await files.rig.api.get(f"{files.base}/stat", params={"path": "~/link"})).json()
        assert link["type"] == "link"
        assert link["link_target"] == ("a.txt" if new else None)


async def test_list_of_a_file(files: Files):
    files.put("~/a.txt")
    response = await files.rig.api.get(f"{files.base}/list", params={"path": "~/a.txt"})
    assert (response.status_code, response.json()["error"]) == (409, "sftp_not_a_directory")
    assert response.json()["detail"] == f"{HOME}/a.txt is not a directory"


async def test_mkdir(files: Files):
    made = await files.rig.api.post(f"{files.base}/mkdir", json={"path": "new"})
    assert made.status_code == 201, made.text
    assert (made.json()["path"], made.json()["type"]) == (f"{HOME}/new", "dir")
    assert files.server("~/new").is_dir()

    again = await files.rig.api.post(f"{files.base}/mkdir", json={"path": "new"})
    assert (again.status_code, again.json()["error"]) == (409, "sftp_target_exists")
    deep = await files.rig.api.post(f"{files.base}/mkdir", json={"path": "a/b/c"})
    assert (deep.status_code, deep.json()["error"]) == (404, "sftp_not_found")
    assert "parents=true" in deep.json()["detail"]
    deep = await files.rig.api.post(f"{files.base}/mkdir", json={"path": "a/b/c", "parents": True})
    assert deep.status_code == 201, deep.text
    assert files.server("~/a/b/c").is_dir()
    files.put("~/file")
    under_file = await files.rig.api.post(f"{files.base}/mkdir", json={"path": "file/x"})
    assert (under_file.status_code, under_file.json()["error"]) == (409, "sftp_not_a_directory")


async def test_remove(files: Files):
    files.put("~/a.txt")
    files.put("~/full/sub/deep.txt")
    files.server("~/empty").mkdir()
    api = files.rig.api

    removed = await api.delete(f"{files.base}/entry", params={"path": "a.txt"})
    assert removed.json() == {"path": f"{HOME}/a.txt", "removed": True}
    assert not files.server("~/a.txt").exists()
    assert (await api.delete(f"{files.base}/entry", params={"path": "empty"})).status_code == 200

    not_empty = await api.delete(f"{files.base}/entry", params={"path": "full"})
    assert (not_empty.status_code, not_empty.json()["error"]) == (409, "sftp_directory_not_empty")
    assert files.server("~/full/sub/deep.txt").exists()
    tree = await api.delete(f"{files.base}/entry", params={"path": "full", "recursive": "true"})
    assert tree.status_code == 200 and not files.server("~/full").exists()

    missing = await api.delete(f"{files.base}/entry", params={"path": "a.txt"})
    assert (missing.status_code, missing.json()["error"]) == (404, "sftp_not_found")
    for protected in ("/", "~", HOME, "/home"):
        refused = await api.delete(
            f"{files.base}/entry", params={"path": protected, "recursive": "true"}
        )
        assert (refused.status_code, refused.json()["error"]) == (403, "sftp_protected_path")
    assert files.server("~").is_dir()


async def test_move_and_rename(files: Files):
    files.put("~/a.txt", b"a")
    files.put("~/b.txt", b"b")
    files.put("~/dir/inner.txt")
    api, base = files.rig.api, files.base

    renamed = await api.post(f"{base}/move", json={"source": "a.txt", "target": "c.txt"})
    assert renamed.status_code == 200, renamed.text
    assert (renamed.json()["path"], files.server("~/c.txt").read_bytes()) == (f"{HOME}/c.txt", b"a")

    taken = await api.post(f"{base}/move", json={"source": "c.txt", "target": "b.txt"})
    assert (taken.status_code, taken.json()["error"]) == (409, "sftp_target_exists")
    assert files.server("~/b.txt").read_bytes() == b"b"
    replaced = await api.post(
        f"{base}/move", json={"source": "c.txt", "target": "b.txt", "overwrite": True}
    )
    assert replaced.status_code == 200, replaced.text
    assert files.server("~/b.txt").read_bytes() == b"a"

    moved = await api.post(f"{base}/move", json={"source": "dir", "target": "/tmp/dir"})
    assert moved.status_code == 200, moved.text
    assert files.server("/tmp/dir/inner.txt").exists()

    errors = [
        ({"source": "missing", "target": "x"}, 404, "sftp_not_found"),
        ({"source": "b.txt", "target": "no/such/x"}, 404, "sftp_not_found"),
        ({"source": "b.txt", "target": "/tmp/dir"}, 409, "sftp_is_a_directory"),
        ({"source": "/tmp/dir", "target": "/tmp/dir/x"}, 422, "sftp_bad_path"),
        ({"source": "b.txt", "target": "b.txt"}, 422, "sftp_bad_path"),
        ({"source": "~", "target": "/tmp/home"}, 403, "sftp_protected_path"),
    ]
    for body, status, code in errors:
        response = await api.post(f"{base}/move", json=body)
        assert (response.status_code, response.json()["error"]) == (status, code), body


async def test_move_failure_without_reason_from_the_worker(files: Files):
    files.put("~/a.txt")
    files.rig.worker.sftp_fail["sftp_move_file"] = ""
    response = await files.rig.api.post(
        f"{files.base}/move", json={"source": "a.txt", "target": "b.txt"}
    )
    assert (response.status_code, response.json()["error"]) == (502, "sftp_failed")
    assert "refused to move" in response.json()["detail"]


async def test_copy_files_and_directories(files: Files):
    files.put("~/a.txt", b"a")
    files.put("~/dir/sub/f.txt", b"f")
    api, base = files.rig.api, files.base

    copied = await api.post(f"{base}/copy", json={"source": "a.txt", "target": "copy.txt"})
    assert copied.status_code == 201, copied.text
    assert files.server("~/copy.txt").read_bytes() == b"a"
    tree = await api.post(f"{base}/copy", json={"source": "dir", "target": "dir2"})
    assert tree.status_code == 201, tree.text
    assert tree.json()["type"] == "dir"
    assert files.server("~/dir2/sub/f.txt").read_bytes() == b"f"
    assert files.server("~/dir/sub/f.txt").exists()

    taken = await api.post(f"{base}/copy", json={"source": "a.txt", "target": "copy.txt"})
    assert (taken.status_code, taken.json()["error"]) == (409, "sftp_target_exists")
    into = await api.post(f"{base}/copy", json={"source": "dir", "target": "dir/sub/again"})
    assert (into.status_code, into.json()["error"]) == (422, "sftp_bad_path")


async def test_errors_of_the_worker_become_api_errors(files: Files):
    files.put("~/file.txt")
    api, base = files.rig.api, files.base
    not_dir = await api.get(f"{base}/list", params={"path": "file.txt"})
    assert (not_dir.status_code, not_dir.json()["error"]) == (409, "sftp_not_a_directory")
    missing = await api.get(f"{base}/list", params={"path": "/no/such/dir"})
    assert (missing.status_code, missing.json()["error"]) == (404, "sftp_not_found")
    files.rig.worker.sftp_fail["sftp_list_dir"] = (
        "SSH internal error: Failed to read remote directory: Permission denied: Permission denied"
    )
    denied = await api.get(f"{base}/list", params={"path": "/root"})
    assert (denied.status_code, denied.json()["error"]) == (403, "sftp_permission_denied")
    bad = await api.get(f"{base}/list", params={"path": "bad\x01name"})
    assert (bad.status_code, bad.json()["error"]) == (422, "sftp_bad_path")
    unknown = await api.get("/api/sftp/sessions/0123456789abcdef/list")
    assert (unknown.status_code, unknown.json()["error"]) == (404, "ssh_session_not_found")


async def test_server_without_sftp(files: Files):
    """Сервер без SFTP (в sshd_config нет Subsystem sftp): воркер ждёт около 10 с и
    отвечает "Failed to initialize SFTP: Timeout". UI получает понятную причину, а
    следующие запросы этой сессии не ждут таймаут снова."""
    files.rig.worker.sftp_fail["sftp_list_dir"] = (
        "SSH internal error: Failed to initialize SFTP: Timeout"
    )
    first = await files.rig.api.get(f"{files.base}/list")
    assert (first.status_code, first.json()["error"]) == (502, "sftp_unavailable")
    assert "Subsystem sftp" in first.json()["detail"]
    asked = len(files.requests("sftp_list_dir"))

    again = await files.rig.api.get(f"{files.base}/list", params={"path": "/tmp"})
    assert (again.status_code, again.json()["error"]) == (502, "sftp_unavailable")
    made = await files.rig.api.post(f"{files.base}/mkdir", json={"path": "~/x"})
    assert made.json()["error"] == "sftp_unavailable"
    assert len(files.requests("sftp_list_dir")) == asked  # воркер больше не спрашивали


async def test_worker_without_sftp(files: Files):
    """Старая сборка воркера (например, synex_worker.exe до SFTP) отвечает
    "Unknown action: sftp_list_dir": UI узнаёт, что обновить нужно воркер, а не сервер."""
    files.rig.worker.sftp_fail["sftp_list_dir"] = "Unknown action: sftp_list_dir"
    response = await files.rig.api.get(f"{files.base}/list")
    assert (response.status_code, response.json()["error"]) == (502, "sftp_unavailable")
    assert "synex_worker build has no SFTP" in response.json()["detail"]
    assert "cargo build --release" in response.json()["detail"]


async def test_operations_do_not_reach_the_terminal(files: Files):
    files.put("~/a.txt")
    await files.rig.api.get(f"{files.base}/list")
    await files.rig.api.post(f"{files.base}/mkdir", json={"path": "x"})
    session = files.rig.manager.get(files.rig.session_id)
    assert [entry.kind for entry in session.buffer.since(0).entries] == ["opened"]
    assert session.cwd is None


# ---------------------------------------------------------------- transfers


async def test_upload_and_download_by_path_with_progress(files: Files, monkeypatch):
    files.rig.worker.sftp_delay = 0.03  # на мегабайт: прогресс успевает обновиться
    data = os.urandom(5 * MB + 123)
    (files.local / "big.bin").write_bytes(data)
    files.server("~/docs").mkdir()

    started = await files.rig.api.post(
        f"{files.base}/transfers",
        json={
            "direction": "upload",
            "local_path": str(files.local / "big.bin"),
            "remote_path": "docs",
        },
    )
    assert started.status_code == 202, started.text
    up = started.json()
    assert (up["status"], up["remote_path"], up["bytes_total"]) == (
        "queued",
        f"{HOME}/docs/big.bin",
        len(data),
    )
    done = await files.finished(up["id"])
    assert (done["status"], done["verified"], done["percent"]) == ("completed", True, 100.0), done
    assert done["checksum"] == hashlib.sha256(data).hexdigest()
    assert files.server("~/docs/big.bin").read_bytes() == data
    assert [p.name for p in files.server("~/docs").iterdir()] == ["big.bin"]  # временного нет

    progress = [e for e in files.events if e["id"] == up["id"]]
    statuses = [e["status"] for e in progress]
    assert statuses[0] == "queued" and statuses[-1] == "completed"
    assert "running" in statuses and "verifying" in statuses
    middle = [e["bytes_done"] for e in progress if 0 < e["bytes_done"] < len(data)]
    assert middle, "progress between 0 and 100% was not reported"
    assert [e["bytes_done"] for e in progress] == sorted(e["bytes_done"] for e in progress)

    # Передача шла в служебной сессии: основная во время неё свободна
    uploads = files.requests("sftp_upload_file")
    assert uploads[-1]["id"] != files.rig.session_id
    assert files.rig.manager.companions(files.rig.session_id) == []  # закрыта после

    back = await files.rig.api.post(
        f"{files.base}/transfers",
        json={
            "direction": "download",
            "local_path": str(files.local) + os.sep,
            "remote_path": "~/docs/big.bin",
            "overwrite": True,
        },
    )
    assert back.status_code == 202, back.text
    info = await files.finished(back.json()["id"])
    assert (info["status"], info["verified"]) == ("completed", True), info
    assert info["local_path"] == str(files.local / "big.bin")
    assert (files.local / "big.bin").read_bytes() == data
    assert sorted(p.name for p in files.local.iterdir()) == ["big.bin"]
    middle = [
        e["bytes_done"]
        for e in files.events
        if e["id"] == info["id"] and 0 < e["bytes_done"] < len(data)
    ]
    assert middle


async def test_terminal_works_during_a_transfer(files: Files):
    files.rig.worker.sftp_delay = 0.2
    (files.local / "slow.bin").write_bytes(os.urandom(4 * MB))
    started = await files.rig.api.post(
        f"{files.base}/transfers",
        json={
            "direction": "upload",
            "local_path": str(files.local / "slow.bin"),
            "remote_path": "~",
        },
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
    assert executed.status_code == 200, executed.text
    assert executed.json()["stdout"] == "free"
    running = (await files.rig.api.get(f"/api/sftp/transfers/{transfer_id}")).json()
    assert running["status"] == "running"  # команда не ждала конца передачи
    assert (await files.finished(transfer_id))["status"] == "completed"


async def test_refusals_before_the_transfer_starts(files: Files):
    files.put("~/exists.bin", b"old")
    (files.local / "src.bin").write_bytes(b"new")
    (files.local / "exists.bin").write_bytes(b"local")
    post = files.rig.api.post
    cases = [
        ({"direction": "upload", "local_path": str(files.local / "none.bin"),
          "remote_path": "~"}, 404, "sftp_local_not_found"),
        ({"direction": "upload", "local_path": "relative.bin", "remote_path": "~"},
         422, "sftp_bad_path"),
        ({"direction": "upload", "local_path": str(files.local / "src.bin"),
          "remote_path": "exists.bin"}, 409, "sftp_target_exists"),
        ({"direction": "upload", "local_path": str(files.local / "src.bin"),
          "remote_path": "no/such/dir/"}, 404, "sftp_not_found"),
        ({"direction": "download", "local_path": str(files.local),
          "remote_path": "exists.bin"}, 409, "sftp_local_exists"),
        ({"direction": "download", "local_path": str(files.local),
          "remote_path": "missing.bin"}, 404, "sftp_not_found"),
    ]  # fmt: skip
    for body, status, code in cases:
        response = await post(f"{files.base}/transfers", json=body)
        assert (response.status_code, response.json()["error"]) == (status, code), body
    assert files.server("~/exists.bin").read_bytes() == b"old"
    assert (files.local / "exists.bin").read_bytes() == b"local"
    assert (await files.rig.api.get("/api/sftp/transfers")).json() == []


async def test_overwrite_replaces_only_after_the_transfer(files: Files):
    files.put("~/report.bin", b"old")
    (files.local / "report.bin").write_bytes(b"new version")
    started = await files.rig.api.post(
        f"{files.base}/transfers",
        json={
            "direction": "upload",
            "local_path": str(files.local / "report.bin"),
            "remote_path": "report.bin",
            "overwrite": True,
        },
    )
    info = await files.finished(started.json()["id"])
    assert info["status"] == "completed", info
    assert files.server("~/report.bin").read_bytes() == b"new version"


async def test_old_file_comes_back_if_the_new_one_cannot_be_put_in_place(files: Files):
    """SFTP-переименование не заменяет файл: старый уходит во временную папку, и если
    новый поставить не вышло, старый возвращается на место, а не пропадает."""
    files.put("~/report.bin", b"old")
    (files.local / "report.bin").write_bytes(b"new version")

    def refuse_new_file(action: str, payload: dict) -> str | None:
        source = str(payload.get("remote_path") or "")
        if action == "sftp_move_file" and source.endswith(".part/report.bin"):
            return "SSH internal error: Failed to rename: Failure: Failure"
        return None

    files.rig.worker.sftp_fail_if = refuse_new_file
    started = await files.rig.api.post(
        f"{files.base}/transfers",
        json={
            "direction": "upload",
            "local_path": str(files.local / "report.bin"),
            "remote_path": "report.bin",
            "overwrite": True,
        },
    )
    info = await files.finished(started.json()["id"])
    assert (info["status"], info["error"]) == ("failed", "sftp_failed"), info
    assert "Cannot put the file" in info["detail"]
    assert files.server("~/report.bin").read_bytes() == b"old"
    assert [p.name for p in files.server("~").iterdir()] == ["report.bin"]  # без мусора


async def test_failed_upload_leaves_no_partial_file(files: Files):
    files.put("~/keep.bin", b"keep")
    (files.local / "keep.bin").write_bytes(os.urandom(2 * MB))
    files.rig.worker.sftp_fail["sftp_upload_file"] = (
        "SSH internal error: Failed to upload file: Failure"
    )
    started = await files.rig.api.post(
        f"{files.base}/transfers",
        json={
            "direction": "upload",
            "local_path": str(files.local / "keep.bin"),
            "remote_path": "keep.bin",
            "overwrite": True,
        },
    )
    info = await files.finished(started.json()["id"])
    assert (info["status"], info["error"]) == ("failed", "sftp_failed")
    assert files.server("~/keep.bin").read_bytes() == b"keep"  # старый файл цел
    assert [p.name for p in files.server("~").iterdir()] == ["keep.bin"]


async def test_corrupted_upload_is_not_saved(files: Files):
    (files.local / "c.bin").write_bytes(b"data")

    async def wrong_checksum(session_id: str, path: str) -> str:
        return "0" * 64

    files.service.remote.checksum = wrong_checksum  # type: ignore[method-assign]
    started = await files.rig.api.post(
        f"{files.base}/transfers",
        json={"direction": "upload", "local_path": str(files.local / "c.bin"), "remote_path": "~"},
    )
    info = await files.finished(started.json()["id"])
    assert (info["status"], info["error"]) == ("failed", "sftp_transfer_corrupted")
    assert list(files.server("~").iterdir()) == []


async def test_without_sha256_on_the_server_only_the_size_is_checked(files: Files):
    files.rig.worker.shell.programs.pop("sha256sum")  # type: ignore[union-attr]
    (files.local / "s.bin").write_bytes(b"data")
    started = await files.rig.api.post(
        f"{files.base}/transfers",
        json={"direction": "upload", "local_path": str(files.local / "s.bin"), "remote_path": "~"},
    )
    info = await files.finished(started.json()["id"])
    assert (info["status"], info["verified"]) == ("completed", None)
    assert (await files.rig.api.get(f"{files.base}/info")).json()["checksum"] is False


async def test_cancel_a_running_and_a_queued_transfer(files: Files):
    files.rig.worker.sftp_delay = 0.2
    (files.local / "a.bin").write_bytes(os.urandom(10 * MB))
    (files.local / "b.bin").write_bytes(os.urandom(MB))
    body = {"direction": "upload", "remote_path": "~"}
    first = (
        await files.rig.api.post(
            f"{files.base}/transfers", json={**body, "local_path": str(files.local / "a.bin")}
        )
    ).json()
    second = (
        await files.rig.api.post(
            f"{files.base}/transfers", json={**body, "local_path": str(files.local / "b.bin")}
        )
    ).json()
    async with asyncio.timeout(10):
        while (await files.rig.api.get(f"/api/sftp/transfers/{first['id']}")).json()[
            "bytes_done"
        ] == 0:
            await asyncio.sleep(0.02)
    listed = (await files.rig.api.get("/api/sftp/transfers", params={"active": "true"})).json()
    assert [(t["id"], t["status"]) for t in listed] == [
        (first["id"], "running"),
        (second["id"], "queued"),  # передачи одной сессии идут по очереди
    ]

    cancelled = await files.rig.api.delete(f"/api/sftp/transfers/{second['id']}")
    assert cancelled.json()["status"] == "cancelled"
    running = await files.rig.api.delete(f"/api/sftp/transfers/{first['id']}")
    assert running.status_code == 200
    info = await files.finished(first["id"])
    assert (info["status"], info["error"]) == ("cancelled", "sftp_transfer_cancelled")
    await asyncio.sleep(0.3)  # уборка временной папки после обрыва
    assert list(files.server("~").iterdir()) == []

    forgotten = await files.rig.api.delete(f"/api/sftp/transfers/{first['id']}")
    assert forgotten.status_code == 200
    gone = await files.rig.api.get(f"/api/sftp/transfers/{first['id']}")
    assert (gone.status_code, gone.json()["error"]) == (404, "sftp_transfer_not_found")


async def test_closing_the_session_stops_its_transfers(files: Files):
    files.rig.worker.sftp_delay = 0.2
    (files.local / "a.bin").write_bytes(os.urandom(10 * MB))
    started = (
        await files.rig.api.post(
            f"{files.base}/transfers",
            json={
                "direction": "upload",
                "local_path": str(files.local / "a.bin"),
                "remote_path": "~",
            },
        )
    ).json()
    async with asyncio.timeout(10):
        while (await files.rig.api.get(f"/api/sftp/transfers/{started['id']}")).json()[
            "status"
        ] != "running":
            await asyncio.sleep(0.02)
    await files.rig.manager.close(files.rig.session_id)
    await files.service.on_session_event({"action": "closed", "session_id": files.rig.session_id})
    info = await files.finished(started["id"])
    assert info["status"] in ("cancelled", "failed")
    assert files.rig.manager.companions(files.rig.session_id) == []


async def test_upload_with_the_request_body(files: Files):
    data = os.urandom(300_000)
    response = await files.rig.api.post(
        f"{files.base}/upload", params={"path": "~/up.bin"}, content=data
    )
    assert response.status_code == 201, response.text
    info = response.json()
    assert (info["status"], info["source"], info["local_path"]) == ("completed", "http", None)
    assert (info["bytes_done"], info["checksum"]) == (len(data), hashlib.sha256(data).hexdigest())
    assert files.server("~/up.bin").read_bytes() == data

    into_dir = await files.rig.api.post(
        f"{files.base}/upload", params={"path": "~/", "name": "named.bin"}, content=b"n"
    )
    assert into_dir.status_code == 201, into_dir.text
    assert files.server("~/named.bin").read_bytes() == b"n"
    # Тело запроса лежит у backend под своим именем, а не под именем с сервера
    sent = files.requests("sftp_upload_file")[-1]["payload"]
    assert Path(sent["local_path"]).name == "file.synex-spool"
    assert sent["remote_path"].endswith("/named.bin")
    taken = await files.rig.api.post(f"{files.base}/upload", params={"path": "up.bin"}, content=b"")
    assert (taken.status_code, taken.json()["error"]) == (409, "sftp_target_exists")


@pytest.mark.skipif(os.name == "nt", reason="the fake server keeps its files on this disk")
async def test_upload_with_the_request_body_under_a_name_windows_does_not_allow(files: Files):
    """Имя, которое Windows не даёт файлам: тело запроса лежит у backend под своим
    именем, поэтому на сервер файл попадает под тем, что просили."""
    odd = await files.rig.api.post(
        f"{files.base}/upload", params={"path": "~/", "name": "report?v2.txt"}, content=b"q"
    )
    assert odd.status_code == 201, odd.text
    assert files.server("~/report?v2.txt").read_bytes() == b"q"


async def test_failed_upload_with_the_request_body_answers_with_the_error(files: Files):
    files.rig.worker.sftp_fail["sftp_upload_file"] = (
        "SSH internal error: Failed to create remote file: Permission denied: Permission denied"
    )
    response = await files.rig.api.post(
        f"{files.base}/upload", params={"path": "~/x.bin"}, content=b"data"
    )
    assert (response.status_code, response.json()["error"]) == (403, "sftp_permission_denied")
    (failed,) = (await files.rig.api.get("/api/sftp/transfers")).json()
    assert failed["status"] == "failed"


@needs_unicode_file_names
async def test_download_with_the_response_body(files: Files):
    data = os.urandom(2 * MB + 77)
    name = "отчёт за май.bin"
    files.put(f"~/{name}", data)
    response = await files.rig.api.get(f"{files.base}/download", params={"path": f"~/{name}"})
    assert response.status_code == 200, response.text
    assert response.content == data
    assert response.headers["content-type"] == "application/octet-stream"
    assert f"filename*=UTF-8''{quote(name)}" in response.headers["content-disposition"]
    info = (
        await files.rig.api.get(f"/api/sftp/transfers/{response.headers[TRANSFER_ID_HEADER]}")
    ).json()
    assert (info["status"], info["source"], info["verified"]) == ("completed", "http", True)

    missing = await files.rig.api.get(f"{files.base}/download", params={"path": "missing"})
    assert (missing.status_code, missing.json()["error"]) == (404, "sftp_not_found")


@pytest.mark.skipif(os.name == "nt", reason="the fake server keeps its files on this disk")
async def test_names_this_computer_cannot_use(files: Files, monkeypatch):
    """Имя "log:2026.txt" на Linux-сервере обычное, а на Windows файла с таким именем не
    создать. Скачивание телом ответа идёт (у backend файл лежит под своим именем), а
    скачивание в папку этого компьютера отказывает понятной ошибкой до передачи."""
    from app.plugins.sftp import local

    files.put("~/log:2026.txt", b"log")
    response = await files.rig.api.get(f"{files.base}/download", params={"path": "~/log:2026.txt"})
    assert response.status_code == 200, response.text
    assert response.content == b"log"
    assert "filename*=UTF-8''log%3A2026.txt" in response.headers["content-disposition"]

    # Как на Windows: имена проверяются по её правилам
    windows_rules = local.name_problem
    monkeypatch.setattr(local, "name_problem", lambda name: windows_rules(name, windows=True))
    refused = await files.rig.api.post(
        f"{files.base}/transfers",
        json={
            "direction": "download",
            "local_path": str(files.local),
            "remote_path": "log:2026.txt",
        },
    )
    assert (refused.status_code, refused.json()["error"]) == (422, "sftp_bad_path")
    assert "Windows does not allow" in refused.json()["detail"]
    renamed = await files.rig.api.post(
        f"{files.base}/transfers",
        json={
            "direction": "download",
            "local_path": str(files.local / "log-2026.txt"),
            "remote_path": "log:2026.txt",
        },
    )
    assert renamed.status_code == 202, renamed.text
    assert (await files.finished(renamed.json()["id"]))["status"] == "completed"
    assert (files.local / "log-2026.txt").read_bytes() == b"log"


async def test_progress_reaches_the_ws_hub(files: Files):
    """В приложении события передач идут в шину, а хаб пересылает их в /ws, пока плагин
    включён (BasePlugin.events)."""
    from fastapi import APIRouter

    from app.plugins.manager import PluginManager
    from app.ws import WebSocketHub

    hub = WebSocketHub()
    manager = PluginManager(forwarder=hub)
    manager.discover(APIRouter(), enabled=["sftp"])
    await manager.load_all()
    try:
        assert hub.streams == ["ssh.session", "sftp.transfer"]
    finally:
        await manager.unload_all()
    assert hub.streams == []
    assert service._service is None


# ----------------------------------------------------------- this computer


async def test_local_listing(files: Files):
    (files.local / "docs").mkdir()
    (files.local / "b.txt").write_bytes(b"12345")
    response = await files.rig.api.get("/api/sftp/local/list", params={"path": str(files.local)})
    assert response.status_code == 200, response.text
    listing = response.json()
    assert (listing["path"], listing["parent"], listing["separator"]) == (
        str(files.local),
        str(files.local.parent),
        os.sep,
    )
    assert [(e["name"], e["type"]) for e in listing["entries"]] == [
        ("docs", "dir"),
        ("b.txt", "file"),
    ]
    assert listing["entries"][1]["modified_at"] is not None
    relative = await files.rig.api.get("/api/sftp/local/list", params={"path": "relative"})
    assert (relative.status_code, relative.json()["error"]) == (422, "sftp_bad_path")


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        ("sftp_not_found: Failed to read remote directory: No such file", "sftp_not_found"),
        ("sftp_permission_denied: Failed to create remote file: Permission denied",
         "sftp_permission_denied"),
        ("sftp_exists: destination already exists: /a", "sftp_target_exists"),
        ("sftp_not_a_directory: not a directory: /a", "sftp_not_a_directory"),
        ("sftp_unavailable: server refused SFTP subsystem: x", "sftp_unavailable"),
        ("sftp_failed: Failed to copy remote file: cp: boom", "sftp_failed"),
        ("SSH internal error: Failed to open local file: No such file", "sftp_local_error"),
        # сборки до 2026-11-14: только тексты
        ("SSH internal error: Failed to x: No such file: No such file", "sftp_not_found"),
        ("SSH internal error: Failed to x: Permission denied: Permission denied",
         "sftp_permission_denied"),
        ("SSH internal error: Failed to initialize SFTP: Timeout", "sftp_unavailable"),
        ("Unknown action: sftp_list_dir", "sftp_unavailable"),
    ],
)  # fmt: skip
def test_worker_errors(stderr: str, code: str):
    from app.plugins.sftp.remote import _translate

    error = _translate(stderr, {"remote_path": "/a", "destination_path": "/b"})
    assert error.code == code, error.detail
    assert not error.detail.startswith("sftp_")  # код воркера в текст не попадает
