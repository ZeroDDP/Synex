"""Файлы этого компьютера для плагина sftp (вторая панель и локальная сторона передачи).
Тесты идут на Windows, Linux и macOS: пути берутся из tmp_path, а не пишутся руками."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from app.plugins.sftp import local
from app.plugins.sftp.errors import (
    SftpBadPathError,
    SftpIsADirectoryError,
    SftpLocalExistsError,
    SftpLocalNotADirectoryError,
    SftpLocalNotFoundError,
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
