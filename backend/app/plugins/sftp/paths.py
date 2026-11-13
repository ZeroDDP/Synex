"""Пути на сервере (всегда POSIX, как их понимает SFTP).

Пользователь и UI пишут путь как удобно: "/var/log", "docs/a.txt" (от домашней папки),
"~" или "~/docs". Здесь он становится абсолютным и нормализованным: в ответах API пути
всегда абсолютные, и UI передаёт их дальше как есть.
"""

from __future__ import annotations

import posixpath

from app.plugins.sftp.errors import SftpBadPathError

MAX_PATH = 4096


def normalize(path: str | None, home: str) -> str:
    """Абсолютный нормализованный путь. Пустой путь и "~" это домашняя папка."""
    if path is None or path.strip() == "":
        return home
    if len(path) > MAX_PATH:
        raise SftpBadPathError(f"Path is longer than {MAX_PATH} characters")
    if any(ord(ch) < 32 for ch in path):
        raise SftpBadPathError("Path must not contain control characters (line breaks, tabs)")
    if path == "~" or path.startswith("~/"):
        path = home + path[1:]
    elif path.startswith("~"):
        raise SftpBadPathError("Paths like ~user are not supported: use an absolute path")
    if not path.startswith("/"):
        path = posixpath.join(home, path)
    normalized = posixpath.normpath(path)
    # normpath оставляет "//x" как есть (POSIX разрешает), серверу это всё равно "/x"
    if normalized.startswith("//"):
        normalized = "/" + normalized.lstrip("/")
    return normalized


def parent(path: str) -> str | None:
    """Папка, в которой лежит path; None для корня."""
    if path == "/":
        return None
    return posixpath.dirname(path) or "/"


def name(path: str) -> str:
    return posixpath.basename(path) or "/"


def join(directory: str, item: str) -> str:
    return posixpath.join(directory, item)


def is_inside(path: str, directory: str) -> bool:
    """path лежит внутри directory (или совпадает с ней)."""
    return path == directory or path.startswith(directory.rstrip("/") + "/")


def is_protected(path: str, home: str) -> bool:
    """Корень и домашнюю папку (и папки над ней) удалять и перемещать нельзя."""
    return path == "/" or is_inside(home, path)
