"""Дерево папки, которую передают целиком: что в ней передавать и что пропущено.

Одинаковое для обеих сторон: папку этого компьютера обходит local.scan_tree, папку
сервера RemoteFS.tree. Пути внутри дерева (rel) всегда через "/", от корня папки.

Передаются обычные файлы и папки (пустые тоже). Остальное попадает в skipped с причиной:
ссылки (по ним не ходят: ссылка может вести наружу или по кругу), сокеты и устройства,
то, что нельзя прочитать, и имена, которые не годятся другой стороне.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.plugins.sftp.errors import SftpFolderTooLargeError

MAX_DEPTH = 128  # глубже папки не обходятся: защита от бесконечной вложенности

REASON_LINK = "Links are not transferred"
REASON_SPECIAL = "Not a regular file"
REASON_UNREADABLE = "No permission to read"
REASON_DEPTH = f"Nested deeper than {MAX_DEPTH} folders"
REASON_CONTROL = "Control characters in the name"


class TransferStoppedError(Exception):
    """Передачу отменили: работа прекращается на ближайшей проверке."""


@dataclass(frozen=True)
class FileItem:
    name: str
    rel: str
    size: int


@dataclass
class DirItem:
    name: str
    rel: str  # "" у корня
    files: list[FileItem] = field(default_factory=list)
    dirs: list[DirItem] = field(default_factory=list)


@dataclass(frozen=True)
class Skipped:
    path: str  # от корня папки; у папки с "/" в конце
    reason: str


@dataclass
class Tree:
    root: DirItem
    files: int = 0
    dirs: int = 0  # без корня
    size: int = 0  # сумма размеров файлов
    skipped: list[Skipped] = field(default_factory=list)


class TreeBuilder:
    """Счётчики обхода: общий лимит и отмена."""

    def __init__(self, name: str, limit: int, stopped: Callable[[], bool]) -> None:
        self.tree = Tree(root=DirItem(name=name, rel=""))
        self.limit = limit
        self.stopped = stopped

    def check(self) -> None:
        if self.stopped():
            raise TransferStoppedError
        if self.tree.files + self.tree.dirs > self.limit:
            raise SftpFolderTooLargeError(
                f"The folder has more than {self.limit} files and folders "
                "(SFTP_FOLDER_LIMIT): transfer it in parts"
            )

    def add_file(self, parent: DirItem, name: str, size: int) -> None:
        parent.files.append(FileItem(name=name, rel=join(parent.rel, name), size=size))
        self.tree.files += 1
        self.tree.size += size
        self.check()

    def add_dir(self, parent: DirItem, name: str) -> DirItem:
        item = DirItem(name=name, rel=join(parent.rel, name))
        parent.dirs.append(item)
        self.tree.dirs += 1
        self.check()
        return item

    def skip(self, parent: DirItem, name: str, reason: str, *, folder: bool = False) -> None:
        path = join(parent.rel, name) + ("/" if folder else "")
        self.tree.skipped.append(Skipped(path=path, reason=reason))


def join(rel: str, name: str) -> str:
    return f"{rel}/{name}" if rel else name


def depth(rel: str) -> int:
    return rel.count("/") + 1 if rel else 0
