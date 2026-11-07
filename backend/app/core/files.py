"""Файлы с секретами в папке данных пользователя (keys.json, connections.json)."""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path


def write_private_file(path: Path, data: str) -> None:
    """Атомарная запись (временный файл + os.replace): при сбое остаётся старый файл.

    На Linux/macOS у файла права 0600, у папки 0700. На Windows файл наследует права
    профиля пользователя (%LOCALAPPDATA%).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    restrict_dir(path.parent)
    tmp = path.with_name(f"{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)


def restrict_dir(path: Path) -> None:
    """Папка только для владельца (Linux/macOS)."""
    if sys.platform != "win32":
        with contextlib.suppress(OSError):
            path.chmod(0o700)
