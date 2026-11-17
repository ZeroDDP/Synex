"""Файлы этого компьютера для плагина sftp (вторая панель и локальная сторона передачи).
Тесты идут на Windows, Linux и macOS: пути берутся из tmp_path, а не пишутся руками."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from app.plugins.sftp import local, tree
from app.plugins.sftp.errors import (
    SftpBadPathError,
    SftpFolderTooLargeError,
    SftpIsADirectoryError,
    SftpLocalError,
    SftpLocalExistsError,
    SftpLocalNotADirectoryError,
    SftpLocalNotFoundError,
    SftpLocalPermissionError,
    SftpNotAFileError,
)
from tests.conftest import needs_unicode_file_names


def make_symlink(link: Path, target: Path | str) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here (Windows without developer mode)")


# ------------------------------------------------------------------ resolve


def test_empty_path_and_tilde_are_the_home_directory():
    assert local.resolve(None) == Path.home()
    assert local.resolve("") == Path.home()
    assert local.resolve("~") == Path.home()
    assert local.resolve(os.path.join("~", "docs")) == Path.home() / "docs"


def test_path_is_normalized_without_touching_the_disk(tmp_path: Path):
    messy = os.path.join(str(tmp_path), "a", "..", "b", ".", "c")
    assert local.resolve(messy) == tmp_path / "b" / "c"
    assert local.resolve(str(tmp_path) + os.sep) == tmp_path


@pytest.mark.parametrize("path", ["relative.txt", "dir/file.txt", "./x", "..", "bad\x00name"])
def test_relative_and_broken_paths_are_refused(path: str):
    with pytest.raises(SftpBadPathError):
        local.resolve(path)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows paths")
def test_windows_paths():
    assert local.resolve("C:\\Users\\..\\Windows") == Path("C:\\Windows")
    assert local.resolve("C:/Users/Public") == Path("C:\\Users\\Public")
    for path in ("C:file.txt", "\\no-drive", "file.txt"):
        with pytest.raises(SftpBadPathError):
            local.resolve(path)
    assert "C:\\" in local.roots()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths")
def test_posix_roots():
    assert local.roots() == ["/"]
    assert local.list_dir("/").parent is None


# ------------------------------------------------------------------ listing


def test_listing_puts_directories_first_and_ignores_case(tmp_path: Path):
    (tmp_path / "zeta").mkdir()
    (tmp_path / "Alpha").mkdir()
    (tmp_path / "b.txt").write_bytes(b"12345")
    (tmp_path / "A.txt").write_bytes(b"")
    (tmp_path / ".config").write_bytes(b"x")

    listing = local.list_dir(str(tmp_path))

    assert listing.path == str(tmp_path)
    assert listing.parent == str(tmp_path.parent)
    assert listing.separator == os.sep
    assert listing.home == str(Path.home())
    assert [(e.name, e.type) for e in listing.entries] == [
        ("Alpha", "dir"),
        ("zeta", "dir"),
        (".config", "file"),
        ("A.txt", "file"),
        ("b.txt", "file"),
    ]
    entries = {e.name: e for e in listing.entries}
    file = entries["b.txt"]
    assert (file.size, file.path, file.is_dir, file.hidden) == (
        5,
        str(tmp_path / "b.txt"),
        False,
        False,
    )
    assert file.modified_at is not None and file.modified_at.tzinfo is not None
    assert file.permissions is not None and len(file.permissions) == 10
    assert entries[".config"].hidden is True
    assert entries["Alpha"].is_dir is True


@needs_unicode_file_names
def test_listing_of_names_that_are_not_ascii(tmp_path: Path):
    names = ["отчёт за май.txt", "日本語.bin", "with space & (brackets).txt"]
    for name in names:
        (tmp_path / name).write_bytes(b"x")
    assert sorted(e.name for e in local.list_dir(str(tmp_path)).entries) == sorted(names)


def test_listing_shows_where_links_lead(tmp_path: Path):
    (tmp_path / "dir").mkdir()
    (tmp_path / "file.txt").write_text("x")
    make_symlink(tmp_path / "to-dir", tmp_path / "dir")
    make_symlink(tmp_path / "to-file", tmp_path / "file.txt")
    make_symlink(tmp_path / "broken", tmp_path / "nowhere")

    entries = {e.name: e for e in local.list_dir(str(tmp_path)).entries}

    assert (entries["to-dir"].type, entries["to-dir"].target_type, entries["to-dir"].is_dir) == (
        "link",
        "dir",
        True,
    )
    assert (entries["to-file"].type, entries["to-file"].target_type) == ("link", "file")
    assert (entries["broken"].type, entries["broken"].target_type) == ("link", None)
    # Ссылки на папки стоят вместе с папками
    assert [e.name for e in local.list_dir(str(tmp_path)).entries][:2] == ["dir", "to-dir"]


def test_listing_errors(tmp_path: Path):
    (tmp_path / "file.txt").write_text("x")
    with pytest.raises(SftpLocalNotFoundError):
        local.list_dir(str(tmp_path / "missing"))
    with pytest.raises(SftpLocalNotADirectoryError):
        local.list_dir(str(tmp_path / "file.txt"))
    with pytest.raises(SftpBadPathError):
        local.list_dir("relative")


# --------------------------------------------------------- transfer endpoints


def test_source_file(tmp_path: Path):
    file = tmp_path / "data.bin"
    file.write_bytes(b"12345")
    assert local.source_file(str(file)) == (file, 5)
    with pytest.raises(SftpLocalNotFoundError):
        local.source_file(str(tmp_path / "missing.bin"))
    with pytest.raises(SftpIsADirectoryError):
        local.source_file(str(tmp_path))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes here")
def test_source_must_be_a_regular_file(tmp_path: Path):
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(SftpNotAFileError):
        local.source_file(str(tmp_path / "pipe"))


def test_target_file(tmp_path: Path):
    (tmp_path / "dir").mkdir()
    (tmp_path / "exists.bin").write_bytes(b"old")

    # Папка: файл ляжет в неё под именем с сервера
    assert local.target_file(str(tmp_path), "new.bin", overwrite=False) == tmp_path / "new.bin"
    assert (
        local.target_file(str(tmp_path / "other.bin"), "new.bin", overwrite=False)
        == tmp_path / "other.bin"
    )
    with pytest.raises(SftpLocalExistsError):
        local.target_file(str(tmp_path / "exists.bin"), "x", overwrite=False)
    with pytest.raises(SftpLocalExistsError):
        local.target_file(str(tmp_path), "exists.bin", overwrite=False)
    assert local.target_file(str(tmp_path), "exists.bin", overwrite=True) == tmp_path / "exists.bin"
    with pytest.raises(SftpIsADirectoryError):
        local.target_file(str(tmp_path), "dir", overwrite=True)
    with pytest.raises(SftpLocalNotFoundError):
        local.target_file(str(tmp_path / "no" / "such" / "file.bin"), "x", overwrite=False)


def test_part_file_is_next_to_the_target(tmp_path: Path):
    part = local.part_file(tmp_path / "archive.tar.gz", "3fa2b1c4d5e6")
    assert part == tmp_path / "archive.tar.gz.3fa2b1c4d5e6.synex-part"


@pytest.mark.parametrize(
    ("name", "on_windows", "elsewhere"),
    [
        ("report.txt", None, None),
        ("отчёт за май.txt", None, None),
        ("log:2026.txt", "characters : that Windows does not allow", None),
        ("a\\b", "characters \\ that Windows does not allow", None),
        ('what?"*.txt', 'characters " * ? that Windows does not allow', None),
        ("con", "a name reserved by Windows", None),
        ("NUL.tar.gz", "a name reserved by Windows", None),
        ("COM1 .txt", "a name reserved by Windows", None),
        ("console.log", None, None),
        ("notes.", "a space or a dot at the end, which Windows drops", None),
        ("tab\there", "control characters", "control characters"),
        ("..", "an empty or special name", "an empty or special name"),
    ],
)
def test_names_from_the_server(name, on_windows, elsewhere):
    assert local.name_problem(name, windows=True) == on_windows
    assert local.name_problem(name, windows=False) == elsewhere


def test_download_into_a_folder_refuses_a_name_this_computer_cannot_use(tmp_path: Path):
    with pytest.raises(SftpBadPathError, match="cannot be used on this computer"):
        local.target_file(str(tmp_path), "bad\x01name", overwrite=False)
    # Явное имя файла снимает вопрос
    assert local.target_file(str(tmp_path / "good.bin"), "bad\x01name", overwrite=False) == (
        tmp_path / "good.bin"
    )


# ------------------------------------------------------------------ folders


def write_files(root: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        path = root.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def scan(root: Path, limit: int = 1000, stopped: bool = False) -> tree.Tree:
    return local.scan_tree(root, limit=limit, stopped=lambda: stopped)


def flat(item: tree.DirItem) -> list[str]:
    """Пути дерева по порядку обхода: файлы папки, потом её папки."""
    paths = [file.rel for file in item.files]
    for sub in item.dirs:
        paths.append(sub.rel + "/")
        paths.extend(flat(sub))
    return paths


def test_scan_tree(tmp_path: Path):
    root = tmp_path / "project"
    write_files(root, {"b.txt": b"12", "a.txt": b"1", "src/main.py": b"123", "src/x/y.bin": b""})
    (root / "empty").mkdir()
    found = scan(root)
    assert found.root.name == "project" and found.root.rel == ""
    assert flat(found.root) == [
        "a.txt", "b.txt", "empty/", "src/", "src/main.py", "src/x/", "src/x/y.bin",
    ]  # fmt: skip
    assert (found.files, found.dirs, found.size, found.skipped) == (4, 3, 6, [])
    assert [file.size for file in found.root.files] == [1, 2]


def test_scan_tree_skips_links(tmp_path: Path):
    root = tmp_path / "project"
    write_files(root, {"a.txt": b"a", "real/b.txt": b"b"})
    make_symlink(root / "to-file", "a.txt")
    make_symlink(root / "to-dir", root / "real")
    make_symlink(root / "loop", root)
    found = scan(root)
    assert flat(found.root) == ["a.txt", "real/", "real/b.txt"]
    assert found.skipped == [
        tree.Skipped("loop", tree.REASON_LINK),
        tree.Skipped("to-dir", tree.REASON_LINK),
        tree.Skipped("to-file", tree.REASON_LINK),
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions and special files")
def test_scan_tree_skips_what_cannot_be_read(tmp_path: Path):
    root = tmp_path / "project"
    write_files(root, {"a.txt": b"a", "closed/x.txt": b"x", "secret.txt": b"s", "tab\tname": b"t"})
    os.mkfifo(root / "pipe")
    os.chmod(root / "closed", 0)
    os.chmod(root / "secret.txt", 0)
    try:
        found = scan(root)
    finally:
        os.chmod(root / "closed", 0o755)
    skipped = {item.path: item.reason for item in found.skipped}
    assert skipped.pop("pipe") == tree.REASON_SPECIAL
    assert skipped.pop("tab\tname") == tree.REASON_CONTROL
    if os.geteuid() != 0:  # root читает всё
        assert skipped.pop("closed/") == tree.REASON_UNREADABLE
        assert skipped.pop("secret.txt") == tree.REASON_UNREADABLE
        assert flat(found.root) == ["a.txt"]
    assert skipped == {}


def test_scan_tree_limits(tmp_path: Path, monkeypatch):
    root = tmp_path / "project"
    write_files(root, {f"d{i}/f.txt": b"x" for i in range(5)})
    assert scan(root, limit=10).files == 5
    with pytest.raises(SftpFolderTooLargeError) as caught:
        scan(root, limit=9)
    assert "more than 9 files and folders" in caught.value.detail
    with pytest.raises(tree.TransferStoppedError):
        scan(root, stopped=True)

    deep = tmp_path / "deep"
    write_files(deep, {"1/2/3/4/file.txt": b"x", "1/top.txt": b"x"})
    monkeypatch.setattr(local, "MAX_DEPTH", 2)
    found = scan(deep)
    assert flat(found.root) == ["1/", "1/top.txt", "1/2/"]
    assert found.skipped == [tree.Skipped("1/2/3/", local.REASON_DEPTH)]


def test_source_dir(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "file.txt").write_bytes(b"x")
    assert local.source_dir(str(tmp_path / "docs")) == tmp_path / "docs"
    assert local.is_dir(str(tmp_path / "docs")) is True
    assert local.is_dir(str(tmp_path / "file.txt")) is False
    assert local.is_dir(str(tmp_path / "missing")) is False
    with pytest.raises(SftpLocalNotFoundError):
        local.source_dir(str(tmp_path / "missing"))
    with pytest.raises(SftpLocalNotADirectoryError):
        local.source_dir(str(tmp_path / "file.txt"))
    with pytest.raises(SftpBadPathError, match="whole disk"):
        local.source_dir(Path(tmp_path.anchor).as_posix() if os.name != "nt" else tmp_path.anchor)


def test_target_dir(tmp_path: Path):
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "site").mkdir()
    (tmp_path / "file.txt").write_bytes(b"x")
    # Новое имя: папка появится под ним
    assert local.target_dir(str(tmp_path / "new"), "site", overwrite=False) == (
        tmp_path / "new",
        False,
    )
    # Существующая папка: «в неё под именем с сервера»
    assert local.target_dir(str(tmp_path), "site", overwrite=False) == (tmp_path / "site", False)
    with pytest.raises(SftpLocalExistsError, match="overwrite=true to merge"):
        local.target_dir(str(tmp_path / "taken"), "site", overwrite=False)
    assert local.target_dir(str(tmp_path / "taken"), "site", overwrite=True) == (
        tmp_path / "taken" / "site",
        True,
    )
    with pytest.raises(SftpLocalNotADirectoryError):
        local.target_dir(str(tmp_path / "file.txt"), "site", overwrite=True)
    with pytest.raises(SftpLocalNotFoundError):
        local.target_dir(str(tmp_path / "no" / "such"), "site", overwrite=False)
    with pytest.raises(SftpLocalNotFoundError):
        local.target_dir(str(tmp_path / "missing") + os.sep, "site", overwrite=False)
    with pytest.raises(SftpBadPathError):
        local.target_dir(str(tmp_path), "/", overwrite=False)  # корень сервера: имени нет


def test_folder_goes_into_place_whole(tmp_path: Path):
    target = tmp_path / "site"
    part = local.part_dir(target, "abc123")
    assert part == tmp_path / ("site.abc123" + local.PART_SUFFIX)
    local.make_dir(part)
    write_files(part, {"a.txt": b"a", "sub/b.txt": b"b"})
    local.replace_dir(part, target)
    assert not part.exists() and (target / "sub" / "b.txt").read_bytes() == b"b"

    again = local.part_dir(target, "def456")
    local.make_dir(again)
    with pytest.raises(SftpLocalExistsError, match="appeared"):
        local.replace_dir(again, target)  # на месте уже есть папка: не заменяется
    with pytest.raises((SftpLocalError, SftpLocalExistsError, SftpLocalPermissionError)):
        local.make_dir(again)
    write_files(again, {"x/y.txt": b"y"})
    assert local.remove_tree(again) is True and not again.exists()
    assert local.remove_tree(again) is True  # уже нет: тоже убрано
    assert local.remove_file(target / "a.txt") is True
    assert local.remove_file(target / "a.txt") is True
    assert local.remove_file(target / "sub") is False  # папка: не удалена


def test_names_as_the_file_system_tells_them_apart(tmp_path: Path):
    assert local.name_key("README.md", platform="linux") == "README.md"
    assert local.name_key("README.md", platform="win32") == "readme.md"
    # macOS не различает ещё и формы Unicode: "й" одним знаком и "и" с краткой
    one, two = "\u0439", "\u0438\u0306"
    assert local.name_key(one, platform="darwin") == local.name_key(two, platform="darwin")
    assert local.name_key(one, platform="linux") != local.name_key(two, platform="linux")

    write_files(tmp_path, {"File.txt": b"x", "Docs/a.txt": b"a"})
    assert local.taken_names(tmp_path) == {
        local.name_key("File.txt"): False,
        local.name_key("Docs"): True,
    }
    assert local.taken_names(tmp_path / "missing") == {}
