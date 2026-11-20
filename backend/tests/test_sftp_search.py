"""Поиск файлов вкладки Files на этом компьютере: обход папок в Python, идёт на любой ОС."""

from __future__ import annotations

from pathlib import Path

from app.core.config import settings
from app.plugins.sftp import search
from tests.plugin_rig import plugin_rig


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
