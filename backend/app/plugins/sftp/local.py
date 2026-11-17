"""Файлы этого компьютера: вторая панель файлового менеджера и локальная сторона
передачи. UI работает в окне браузера и диск не видит, поэтому папки ему показывает
backend. Пути здесь в формате этой ОС (C:\\Users\\me на Windows, /home/me на Linux и
macOS), только абсолютные; "~" это домашняя папка."""

from __future__ import annotations

import os
import shutil
import stat
import string
import sys
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from app.plugins.sftp.errors import (
    SftpBadPathError,
    SftpIsADirectoryError,
    SftpLocalError,
    SftpLocalExistsError,
    SftpLocalNotADirectoryError,
    SftpLocalNotFoundError,
    SftpLocalPermissionError,
    SftpNotAFileError,
)
from app.plugins.sftp.tree import (
    MAX_DEPTH,
    REASON_CONTROL,
    REASON_DEPTH,
    REASON_LINK,
    REASON_SPECIAL,
    REASON_UNREADABLE,
    DirItem,
    Tree,
    TreeBuilder,
    depth,
)

PART_SUFFIX = ".synex-part"
LocalType = Literal["file", "dir", "link", "other"]

# Имена, которые Windows не даёт файлам (с любым расширением: NUL.txt тоже нельзя)
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
     *(f"LPT{i}" for i in range(1, 10))}
)  # fmt: skip
_WINDOWS_FORBIDDEN = frozenset('<>:"/\\|?*')


@dataclass(frozen=True)
class LocalEntry:
    name: str
    path: str
    type: LocalType
    size: int | None
    modified_at: datetime | None
    mode: int | None
    target_type: Literal["file", "dir"] | None
    hidden: bool
    link_target: str | None = None

    @property
    def is_dir(self) -> bool:
        return self.type == "dir" or self.target_type == "dir"

    @property
    def permissions(self) -> str | None:
        if self.mode is None:
            return None
        return stat.filemode(self.mode)


@dataclass(frozen=True)
class LocalListing:
    path: str
    parent: str | None
    home: str
    roots: list[str]
    separator: str
    entries: list[LocalEntry]


def resolve(path: str | None) -> Path:
    """Абсолютный нормализованный путь без обращения к диску. Пустой и "~" это
    домашняя папка."""
    if path is None or path.strip() == "":
        return Path.home()
    if "\x00" in path or any(ord(ch) < 32 for ch in path):
        raise SftpBadPathError("Path must not contain control characters")
    if path == "~" or path.startswith(("~/", "~\\")):
        path = str(Path.home()) + path[1:]
    candidate = Path(path)
    # На Windows "C:file" и "\\dir" без диска не абсолютные: текущий диск backend
    # пользователю ни о чём не говорит
    if not candidate.is_absolute():
        raise SftpBadPathError(f"Path on this computer must be absolute: {path}")
    return Path(os.path.normpath(candidate))


def roots() -> list[str]:
    """Диски Windows или "/"."""
    if sys.platform != "win32":
        return ["/"]
    return [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")]


def list_dir(path: str | None) -> LocalListing:
    directory = resolve(path)
    try:
        items = list(os.scandir(directory))
    except FileNotFoundError:
        raise SftpLocalNotFoundError(f"Folder {directory} not found") from None
    except NotADirectoryError:
        raise SftpLocalNotADirectoryError(f"{directory} is not a folder") from None
    except PermissionError:
        raise SftpLocalPermissionError(f"No permission to read {directory}") from None
    except OSError as e:
        if directory.is_file():
            raise SftpLocalNotADirectoryError(f"{directory} is not a folder") from None
        raise SftpLocalError(f"Cannot read {directory}: {e.strerror or e}") from None
    entries = [_entry(item) for item in items]
    entries.sort(key=lambda e: (not e.is_dir, e.name.casefold(), e.name))
    parent = directory.parent
    return LocalListing(
        path=str(directory),
        parent=None if parent == directory else str(parent),
        home=str(Path.home()),
        roots=roots(),
        separator=os.sep,
        entries=entries,
    )


def source_file(path: str) -> tuple[Path, int]:
    """Файл, который загружают на сервер: есть, читается, обычный файл."""
    file = resolve(path)
    try:
        info = file.stat()
    except FileNotFoundError:
        raise SftpLocalNotFoundError(f"File {file} not found on this computer") from None
    except PermissionError:
        raise SftpLocalPermissionError(f"No permission to read {file}") from None
    except OSError as e:
        raise SftpLocalError(f"Cannot read {file}: {e.strerror or e}") from None
    if stat.S_ISDIR(info.st_mode):
        raise SftpIsADirectoryError(f"{file} is a folder, not a file")
    if not stat.S_ISREG(info.st_mode):
        raise SftpNotAFileError(f"{file} is not a regular file")
    if not os.access(file, os.R_OK):
        raise SftpLocalPermissionError(f"No permission to read {file}")
    return file, info.st_size


def is_dir(path: str) -> bool:
    """path на этом компьютере это папка (или ссылка на папку)."""
    try:
        return resolve(path).is_dir()
    except OSError:
        return False


def source_dir(path: str) -> Path:
    """Папка, которую загружают на сервер: есть и читается."""
    folder = resolve(path)
    if not folder.name:
        raise SftpBadPathError(f"{folder} is a whole disk: choose a folder on it")
    try:
        with os.scandir(folder):
            pass
    except FileNotFoundError:
        raise SftpLocalNotFoundError(f"Folder {folder} not found on this computer") from None
    except NotADirectoryError:
        raise SftpLocalNotADirectoryError(f"{folder} is not a folder") from None
    except PermissionError:
        raise SftpLocalPermissionError(f"No permission to read {folder}") from None
    except OSError as e:
        raise SftpLocalError(f"Cannot read {folder}: {e.strerror or e}") from None
    return folder


def scan_tree(root: Path, *, limit: int, stopped: Callable[[], bool]) -> Tree:
    """Обход папки этого компьютера: что в ней загружать (см. tree.py). Работает долго на
    больших папках: вызывать в потоке."""
    builder = TreeBuilder(root.name, limit, stopped)
    try:
        entries = _scandir(root)
    except PermissionError:
        raise SftpLocalPermissionError(f"No permission to read {root}") from None
    except OSError as e:
        raise SftpLocalError(f"Cannot read {root}: {e.strerror or e}") from None
    _scan(builder, builder.tree.root, entries)
    return builder.tree


def _scandir(directory: Path | str) -> list[os.DirEntry[str]]:
    with os.scandir(directory) as items:
        return sorted(items, key=lambda item: item.name)


def _scan(builder: TreeBuilder, item: DirItem, entries: list[os.DirEntry[str]]) -> None:
    for entry in entries:
        builder.check()
        name = entry.name
        try:
            name.encode("utf-8")
        except UnicodeEncodeError:
            builder.skip(item, name.encode("utf-8", "replace").decode(), "The name is not UTF-8")
            continue
        try:
            link = entry.is_symlink() or entry.is_junction()
            folder = not link and entry.is_dir(follow_symlinks=False)
            regular = not link and entry.is_file(follow_symlinks=False)
        except OSError:
            builder.skip(item, name, REASON_UNREADABLE)
            continue
        if any(ord(ch) < 32 for ch in name):
            builder.skip(item, name, REASON_CONTROL, folder=folder)
        elif link:
            builder.skip(item, name, REASON_LINK)
        elif folder:
            if depth(item.rel) + 1 > MAX_DEPTH:
                builder.skip(item, name, REASON_DEPTH, folder=True)
                continue
            try:
                children = _scandir(entry.path)
            except OSError:
                builder.skip(item, name, REASON_UNREADABLE, folder=True)
                continue
            _scan(builder, builder.add_dir(item, name), children)
        elif regular:
            try:
                size = entry.stat(follow_symlinks=False).st_size
            except OSError:
                builder.skip(item, name, REASON_UNREADABLE)
                continue
            if not os.access(entry.path, os.R_OK):
                builder.skip(item, name, REASON_UNREADABLE)
                continue
            builder.add_file(item, name, size)
        else:
            builder.skip(item, name, REASON_SPECIAL)


def name_problem(name: str, *, windows: bool = sys.platform == "win32") -> str | None:
    """Почему имя файла с сервера нельзя дать файлу на этом компьютере (None: можно).
    На Linux и macOS запрещены только "/" и NUL, на Windows намного больше: имя
    "report:v2.txt" или "con.log" на сервере обычное, а здесь такого файла не создать
    (а "a\\b" стал бы путём в папку a)."""
    if name in ("", ".", ".."):
        return "an empty or special name"
    if any(ord(ch) < 32 for ch in name):
        return "control characters"
    if "/" in name:
        return 'the character "/"'
    if windows:
        bad = sorted({ch for ch in name if ch in _WINDOWS_FORBIDDEN})
        if bad:
            return "characters " + " ".join(bad) + " that Windows does not allow"
        if name.endswith((" ", ".")):
            return "a space or a dot at the end, which Windows drops"
        if name.split(".", 1)[0].rstrip().upper() in _WINDOWS_RESERVED:
            return "a name reserved by Windows"
    return None


def target_file(path: str, remote_name: str, *, overwrite: bool) -> Path:
    """Куда скачать: папка (или путь с разделителем в конце) значит «в неё под именем
    с сервера», если это имя годится для файла на этом компьютере."""
    target = resolve(path)
    if path.endswith(("/", "\\")) or target.is_dir():
        if not target.is_dir():
            raise SftpLocalNotFoundError(f"Folder {target} not found on this computer")
        problem = name_problem(remote_name)
        if problem is not None:
            raise SftpBadPathError(
                f"The server file name {remote_name!r} cannot be used on this computer: it "
                f"has {problem}. Pass local_path with another file name"
            )
        target = target / remote_name
    if target.is_dir():
        raise SftpIsADirectoryError(f"{target} is a folder on this computer")
    if target.exists() and not overwrite:
        raise SftpLocalExistsError(
            f"{target} already exists on this computer (pass overwrite=true to replace it)"
        )
    if not target.parent.is_dir():
        raise SftpLocalNotFoundError(f"Folder {target.parent} not found on this computer")
    if not os.access(target.parent, os.W_OK):
        raise SftpLocalPermissionError(f"No permission to write to {target.parent}")
    return target


def target_dir(path: str, remote_name: str, *, overwrite: bool) -> tuple[Path, bool]:
    """Куда скачать папку: (папка, объединять ли с существующей). Существующая папка в
    path (или путь с разделителем в конце) значит «в неё под именем с сервера»."""
    target = resolve(path)
    if path.endswith(("/", "\\")) or target.is_dir():
        if not target.is_dir():
            raise SftpLocalNotFoundError(f"Folder {target} not found on this computer")
        problem = name_problem(remote_name)
        if problem is not None:
            raise SftpBadPathError(
                f"The server folder name {remote_name!r} cannot be used on this computer: it "
                f"has {problem}. Pass local_path with another folder name"
            )
        target = target / remote_name
    if target.is_dir():
        if not overwrite:
            raise SftpLocalExistsError(
                f"Folder {target} already exists on this computer "
                "(pass overwrite=true to merge into it)"
            )
        if not os.access(target, os.W_OK):
            raise SftpLocalPermissionError(f"No permission to write to {target}")
        return target, True
    if target.exists() or target.is_symlink():
        raise SftpLocalNotADirectoryError(
            f"{target} is a file on this computer: a folder cannot replace it"
        )
    if not target.parent.is_dir():
        raise SftpLocalNotFoundError(f"Folder {target.parent} not found on this computer")
    if not os.access(target.parent, os.W_OK):
        raise SftpLocalPermissionError(f"No permission to write to {target.parent}")
    return target, False


def part_dir(target: Path, tag: str) -> Path:
    """Временная папка скачивания рядом с целевой: готовая папка встаёт на место
    переименованием."""
    return target.with_name(f"{target.name}.{tag}{PART_SUFFIX}")


def make_dir(directory: Path) -> None:
    try:
        directory.mkdir()
    except PermissionError:
        raise SftpLocalPermissionError(f"No permission to create {directory}") from None
    except OSError as e:
        raise SftpLocalError(f"Cannot create {directory}: {e.strerror or e}") from None


def replace_dir(part: Path, target: Path) -> None:
    """Готовая временная папка на место целевой (её ещё нет)."""
    if target.exists() or target.is_symlink():
        raise SftpLocalExistsError(f"{target} appeared on this computer during the transfer")
    try:
        os.replace(part, target)
    except PermissionError:
        raise SftpLocalPermissionError(
            f"Cannot save {target}: no permission or a file in it is open in another program"
        ) from None
    except OSError as e:
        raise SftpLocalError(f"Cannot save {target}: {e.strerror or e}") from None


def remove_tree(directory: Path) -> bool:
    """Убрать временную папку с содержимым. False, если что-то осталось (на Windows файл,
    открытый другой программой, не удалить)."""
    shutil.rmtree(directory, ignore_errors=True)
    return not directory.exists()


def remove_file(file: Path) -> bool:
    try:
        file.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def name_key(name: str, *, platform: str = sys.platform) -> str:
    """Как имя различает файловая система этого компьютера: на Windows и macOS регистр
    букв не важен ("README" и "readme" один файл), на macOS ещё и форма Unicode.
    Сравнение с запасом (casefold строже правил NTFS): лучше не скачать файл и сказать
    об этом, чем молча записать его поверх другого."""
    if platform == "darwin":
        return unicodedata.normalize("NFC", name).casefold()
    if platform == "win32":
        return name.casefold()
    return name


def taken_names(directory: Path) -> dict[str, bool]:
    """Что уже лежит в папке: имя (как его различает ФС) -> папка ли это."""
    taken: dict[str, bool] = {}
    try:
        with os.scandir(directory) as items:
            for item in items:
                try:
                    taken[name_key(item.name)] = item.is_dir()
                except OSError:
                    taken[name_key(item.name)] = False
    except OSError:
        pass
    return taken


def part_file(target: Path, tag: str) -> Path:
    """Временный файл скачивания рядом с целевым: на том же диске, поэтому замена
    целевого файла мгновенная. В имени номер передачи: две передачи в один файл не
    пишут друг другу в общий временный, а отменённую воркер может ещё дописывать
    (прерывать передачу он не умеет)."""
    return target.with_name(f"{target.name}.{tag}{PART_SUFFIX}")


def replace(part: Path, target: Path) -> None:
    try:
        os.replace(part, target)
    except PermissionError:
        raise SftpLocalPermissionError(
            f"Cannot replace {target}: no permission or the file is open in another program"
        ) from None
    except OSError as e:
        raise SftpLocalError(f"Cannot save {target}: {e.strerror or e}") from None


def _entry(item: os.DirEntry[str]) -> LocalEntry:
    try:
        info = item.stat(follow_symlinks=False)
    except OSError:
        info = None
    is_link = item.is_symlink()
    target_type = None
    if is_link:
        try:
            target_type = "dir" if item.is_dir(follow_symlinks=True) else "file"
            if not os.path.exists(item.path):
                target_type = None
        except OSError:
            target_type = None
    if is_link:
        kind: LocalType = "link"
    elif item.is_dir(follow_symlinks=False):
        kind = "dir"
    elif item.is_file(follow_symlinks=False):
        kind = "file"
    else:
        kind = "other"
    return LocalEntry(
        name=item.name,
        path=item.path,
        type=kind,
        size=info.st_size if info is not None and kind != "dir" else None,
        modified_at=(datetime.fromtimestamp(info.st_mtime, UTC) if info is not None else None),
        mode=info.st_mode if info is not None else None,
        target_type=target_type,  # type: ignore[arg-type]
        hidden=_hidden(item, info),
        link_target=_link_target(item.path) if is_link else None,
    )


def _link_target(path: str) -> str | None:
    try:
        return os.readlink(path)
    except (OSError, ValueError):
        return None


def _hidden(item: os.DirEntry[str], info: os.stat_result | None) -> bool:
    if item.name.startswith("."):
        return True
    attributes = getattr(info, "st_file_attributes", 0) if info is not None else 0
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0))
