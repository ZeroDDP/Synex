"""Пути на сервере: что пользователь пишет и что уходит в SFTP воркера."""

from __future__ import annotations

import pytest

from app.plugins.sftp import paths
from app.plugins.sftp.errors import SftpBadPathError

HOME = "/home/deploy"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (None, HOME),
        ("", HOME),
        ("  ", HOME),
        ("~", HOME),
        ("~/", HOME),
        ("~/docs/../logs/./a.txt", f"{HOME}/logs/a.txt"),
        ("docs", f"{HOME}/docs"),
        ("../shared", "/home/shared"),
        ("/var/log/", "/var/log"),
        ("//srv//app", "/srv/app"),
        ("/../..", "/"),
        ("/", "/"),
        ("с пробелом и 'кавычкой'", f"{HOME}/с пробелом и 'кавычкой'"),
    ],
)
def test_normalize(path, expected):
    assert paths.normalize(path, HOME) == expected


@pytest.mark.parametrize("path", ["~root/x", "a\nb", "tab\there", "x" * 5000])
def test_bad_paths(path):
    with pytest.raises(SftpBadPathError):
        paths.normalize(path, HOME)


def test_parent_name_join():
    assert paths.parent("/") is None
    assert paths.parent("/var") == "/"
    assert paths.parent("/var/log/syslog") == "/var/log"
    assert (paths.name("/"), paths.name("/var/log")) == ("/", "log")
    assert paths.join("/", "etc") == "/etc"
    assert paths.join("/var", "log") == "/var/log"


def test_protected_and_inside():
    for path in ("/", "/home", HOME):
        assert paths.is_protected(path, HOME)
    for path in (f"{HOME}/docs", "/var", "/home/other"):
        assert not paths.is_protected(path, HOME)
    assert paths.is_inside("/a/b/c", "/a/b") and paths.is_inside("/a/b", "/a/b")
    assert not paths.is_inside("/a/bc", "/a/b")
    assert paths.is_inside("/anything", "/")
