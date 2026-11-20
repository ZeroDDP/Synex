"""Поиск файлов вкладки Files: на сервере (find через SSH) и на этом компьютере.

На сервере — настоящий скрипт поиска в настоящем sh (Linux, macOS), «сервер» — временная
папка теста. На этом компьютере — обход папок в Python, идёт на любой ОС.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.core.config import settings
from app.plugins.sftp import search
from app.plugins.sftp.remote import RemoteFS
from app.plugins.sftp.service import SftpService, get_sftp
from app.plugins.sftp.transfers import TransferManager
from tests.conftest import needs_timeout_utility
from tests.plugin_rig import PluginRig, plugin_rig

needs_sh = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("sh"), reason="needs a real sh"
)


def make_tree(root: Path) -> Path:
    """root/
    ├── a/nginx.conf            "Hello NGINX"
    ├── a/bin.dat               двоичный, внутри есть nginx
    ├── a/b/NGINX.log
    ├── a/b/.git/nginx.cfg      в скрытой папке
    ├── logs/                   пустая папка
    └── nginx/                  папка с подходящим именем
    """
    (root / "a" / "b" / ".git").mkdir(parents=True)
    (root / "nginx").mkdir()
    (root / "logs").mkdir()
    (root / "a" / "nginx.conf").write_bytes(b"Hello NGINX\n")
    (root / "a" / "bin.dat").write_bytes(b"\0binary nginx")
    (root / "a" / "b" / "NGINX.log").write_bytes(b"x\n")
    (root / "a" / "b" / ".git" / "nginx.cfg").write_bytes(b"nginx\n")
    return root


def names(result: search.SearchResult, root: Path) -> list[str]:
    return [Path(item.path).relative_to(root).as_posix() for item in result.entries]


# -------------------------------------------------------------- this computer


def test_local_search_by_name_type_and_depth(tmp_path: Path):
    root = make_tree(tmp_path / "tree")

    def find(**query) -> list[str]:
        return names(search.search_local(str(root), search.SearchQuery(**query)), root)

    # Без учёта регистра, ближние уровни первыми, скрытые папки пропускаются
    assert find(name="nginx") == ["nginx", "a/nginx.conf", "a/b/NGINX.log"]
    assert find(name="nginx", hidden=True) == [
        "nginx",
        "a/nginx.conf",
        "a/b/NGINX.log",
        "a/b/.git/nginx.cfg",
    ]
    assert find(name="*.LOG") == ["a/b/NGINX.log"]
    assert find(name="nginx", type="dir") == ["nginx"]
    assert find(name="nginx", type="file", max_depth=2) == ["a/nginx.conf"]
    # Внутри файлов: двоичные пропускаются
    assert find(contains="hello") == ["a/nginx.conf"]
    assert find(contains="nginx", name="*.conf") == ["a/nginx.conf"]


def test_local_search_limits(tmp_path: Path, monkeypatch):
    root = tmp_path / "many"
    root.mkdir()
    for index in range(5):
        (root / f"file{index}.txt").write_text("text")
    result = search.search_local(str(root), search.SearchQuery(name="file", limit=3))
    assert len(result.entries) == 3 and result.truncated is True

    monkeypatch.setattr(settings, "SFTP_SEARCH_MAX_FILE", 2)
    result = search.search_local(str(root), search.SearchQuery(contains="text"))
    assert result.entries == []  # больше лимита: внутрь не смотрим

    # Время поиска: часы, которые идут на секунду при каждом взгляде на них (настоящие
    # на Windows могут не сдвинуться за весь короткий поиск)
    ticks = iter(range(10_000))
    monkeypatch.setattr(search, "_clock", lambda: float(next(ticks)))
    monkeypatch.setattr(settings, "SFTP_SEARCH_TIMEOUT", 1.5)
    result = search.search_local(str(root), search.SearchQuery(name="file"))
    assert result.timed_out is True
    assert len(result.entries) < 5  # найденное до остановки отдаётся


async def test_local_search_api(tmp_path: Path, missing_socket_path):
    root = make_tree(tmp_path / "tree")
    async with plugin_rig(tmp_path, missing_socket_path, ["sftp"], {}) as rig:
        response = await rig.api.get(
            "/api/sftp/local/search", params={"path": str(root), "name": "conf"}
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["path"] == str(root)
        (entry,) = data["entries"]
        assert entry["name"] == "nginx.conf"
        assert entry["parent"] == str(root / "a")
        assert (entry["type"], entry["is_dir"], entry["size"]) == ("file", False, 12)
        assert entry["modified_at"] is not None
        assert (data["truncated"], data["timed_out"], data["denied"]) == (False, False, 0)

        empty = await rig.api.get("/api/sftp/local/search", params={"path": str(root)})
        assert (empty.status_code, empty.json()["error"]) == (422, "sftp_bad_search")
        slash = await rig.api.get(
            "/api/sftp/local/search", params={"path": str(root), "name": "a/b"}
        )
        assert slash.json()["error"] == "sftp_bad_search"
        missing = await rig.api.get(
            "/api/sftp/local/search", params={"path": str(root / "none"), "name": "x"}
        )
        assert (missing.status_code, missing.json()["error"]) == (404, "sftp_local_not_found")
        relative = await rig.api.get("/api/sftp/local/search", params={"path": "tree", "name": "x"})
        assert relative.status_code == 422


# ---------------------------------------------------------------------- server


def gnu_find() -> bool:
    probe = subprocess.run(["find", ".", "-maxdepth", "0", "-printf", ""], capture_output=True)
    return probe.returncode == 0


@pytest.fixture
async def shell_rig(tmp_path: Path, missing_socket_path) -> AsyncIterator[PluginRig]:
    async with plugin_rig(tmp_path, missing_socket_path, ["sftp"]) as rig:
        remote = RemoteFS(rig.manager)
        sftp = SftpService(remote, TransferManager(remote))
        rig.app.dependency_overrides[get_sftp] = lambda: sftp
        yield rig


@needs_sh
@needs_timeout_utility
async def test_server_search(shell_rig: PluginRig, tmp_path: Path):
    root = make_tree(tmp_path / "tree")
    url = f"/api/sftp/sessions/{shell_rig.session_id}/search"

    async def find(**params) -> list[str]:
        response = await shell_rig.api.get(url, params={"path": str(root), **params})
        assert response.status_code == 200, response.text
        return [Path(e["path"]).relative_to(root).as_posix() for e in response.json()["entries"]]

    assert await find(name="nginx") == ["nginx", "a/nginx.conf", "a/b/NGINX.log"]
    assert await find(name="nginx", hidden="true") == [
        "nginx",
        "a/nginx.conf",
        "a/b/NGINX.log",
        "a/b/.git/nginx.cfg",
    ]
    assert await find(name="*.LOG") == ["a/b/NGINX.log"]
    assert await find(name="nginx", type="dir") == ["nginx"]
    assert await find(name="nginx", type="file", max_depth=2) == ["a/nginx.conf"]
    assert await find(contains="hello") == ["a/nginx.conf"]

    response = await shell_rig.api.get(url, params={"path": str(root), "name": "conf"})
    data = response.json()
    (entry,) = data["entries"]
    assert (entry["name"], entry["parent"]) == ("nginx.conf", str(root / "a"))
    if gnu_find():
        assert (entry["type"], entry["size"]) == ("file", 12)
        assert entry["modified_at"] is not None
    else:  # BSD find (macOS): только пути
        assert (entry["type"], entry["size"]) == (None, None)
    many = await shell_rig.api.get(url, params={"path": str(root), "name": "*", "limit": 2})
    assert len(many.json()["entries"]) == 2 and many.json()["truncated"] is True

    # Поиск шёл в своём SSH-подключении и закрыл его
    assert shell_rig.manager.companions(shell_rig.session_id) == []


@needs_sh
@needs_timeout_utility
async def test_server_search_errors(shell_rig: PluginRig, tmp_path: Path):
    root = make_tree(tmp_path / "tree")
    url = f"/api/sftp/sessions/{shell_rig.session_id}/search"
    missing = await shell_rig.api.get(url, params={"path": str(root / "none"), "name": "x"})
    assert (missing.status_code, missing.json()["error"]) == (404, "sftp_not_found")
    file = await shell_rig.api.get(url, params={"path": str(root / "a/nginx.conf"), "name": "x"})
    assert (file.status_code, file.json()["error"]) == (409, "sftp_not_a_directory")
    empty = await shell_rig.api.get(url, params={"path": str(root)})
    assert (empty.status_code, empty.json()["error"]) == (422, "sftp_bad_search")
    # Без path ищет в домашней папке
    (tmp_path / "home" / "found-me.txt").write_text("1")
    home = await shell_rig.api.get(url, params={"name": "found-me"})
    assert [e["name"] for e in home.json()["entries"]] == ["found-me.txt"]


def test_server_search_without_gnu_find():
    """Не GNU find (BSD, busybox): только пути; тип известен, если искали файлы."""
    stdout = f"{search.HEADER} ok posix\n/srv/a.log\n/srv/x/b.log\n"
    status, result = search.parse_remote(
        stdout, "find: /srv/secret: Permission denied\n", "/srv", search.SearchQuery(name="log")
    )
    assert status == "ok"
    assert [(e.path, e.type, e.size) for e in result.entries] == [
        ("/srv/a.log", None, None),
        ("/srv/x/b.log", None, None),
    ]
    assert result.denied == 1
    _, files = search.parse_remote(stdout, "", "/srv", search.SearchQuery(name="log", type="file"))
    assert {e.type for e in files.entries} == {"file"}
