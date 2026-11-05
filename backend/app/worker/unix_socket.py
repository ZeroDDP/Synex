"""Unix socket воркера на Linux и macOS.

Раньше по умолчанию был /tmp/synex_worker.sock. Общий /tmp не годится:
  * воркеры двух пользователей одной машины спорили бы за один путь;
  * другой пользователь может заранее положить туда свой файл или symlink;
  * после падения воркера файл сокета остаётся, и новый воркер не может занять путь
    (bind: Address already in use) и уходит на TCP.

Поэтому путь по умолчанию лежит в личной папке пользователя (config.default_socket_path),
а перед каждым запуском воркера prepare_socket():
  * создаёт папку synex с правами 0700 и проверяет её: она наша и не symlink. Если нет,
    берёт новую папку через mkdtemp (0700, случайное имя);
  * удаляет сокет, который никто не слушает (остался от упавшего воркера). Живой сокет
    не трогает: его слушает другой процесс;
  * проверяет длину пути: macOS принимает до 103 байт, Linux до 107.

Путь, заданный явно в RUST_IPC_SOCKET_PATH, используется как есть: backend создаёт его
папку, если её нет, но права существующей не меняет; мёртвый сокет удаляет, об общей
папке (вроде /tmp) предупреждает.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import stat
import sys
import tempfile
from pathlib import Path

import structlog

from app.core.config import SOCKET_FILE_NAME

logger = structlog.get_logger()

# sizeof(sockaddr_un.sun_path) минус завершающий ноль
MAX_PATH_BYTES = 103 if sys.platform == "darwin" else 107


class UnsafeSocketDirError(Exception):
    pass


def path_problem(path: Path) -> str | None:
    size = len(os.fsencode(path))
    if size > MAX_PATH_BYTES:
        return f"socket path is {size} bytes long, the limit is {MAX_PATH_BYTES}"
    return None


def ensure_private_dir(directory: Path) -> None:
    """Папка 0700, принадлежит текущему пользователю, не symlink."""
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = os.lstat(directory)
    except OSError as e:
        raise UnsafeSocketDirError(f"cannot create {directory}: {e}") from e
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise UnsafeSocketDirError(f"{directory} is not a directory")
    if info.st_uid != os.getuid():
        raise UnsafeSocketDirError(f"{directory} belongs to another user (uid {info.st_uid})")
    if stat.S_IMODE(info.st_mode) & 0o077:
        try:
            directory.chmod(0o700)
        except OSError as e:
            raise UnsafeSocketDirError(f"cannot chmod 700 {directory}: {e}") from e


def is_listening(path: Path) -> bool:
    """Слушает ли кто-то сокет. При сомнениях (нет прав, таймаут) считаем, что да."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        try:
            probe.connect(os.fspath(path))
        except (ConnectionRefusedError, FileNotFoundError):
            return False
        except OSError:
            return True
    return True


def remove_stale_socket(path: Path) -> None:
    """Удаляет файл сокета, если его никто не слушает."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as e:
        logger.warning("Cannot check worker socket", path=str(path), error=repr(e))
        return
    if not stat.S_ISSOCK(info.st_mode):
        logger.warning(
            "Worker socket path is taken by a file that is not a socket; the worker will "
            "not be able to listen there. Remove it or change RUST_IPC_SOCKET_PATH",
            path=str(path),
        )
        return
    if is_listening(path):
        logger.warning(
            "Another process already listens on the worker socket (a worker left from a "
            "previous run or another backend)",
            path=str(path),
        )
        return
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    logger.info("Removed stale worker socket", path=str(path))


class SocketLocation:
    """Где лежит сокет воркера и кто отвечает за его папку."""

    def __init__(self, configured: str, default: Path) -> None:
        self.explicit = bool(configured)
        self.default = default
        self.path = Path(configured) if configured else default
        self._temp_dir: Path | None = None
        self._checked = False  # папка выбрана и проверена
        self._prepared = False  # был prepare(): есть что убирать в cleanup()

    def choose(self) -> Path:
        """Выбирает и проверяет папку сокета (один раз): см. docstring модуля."""
        if not self._checked:
            self._checked = True
            if self.explicit:
                self._check_explicit()
            else:
                self._choose_private_dir()
        return self.path

    def prepare(self) -> Path:
        """Перед запуском воркера: папка, проверки, удаление мёртвого сокета."""
        self.choose()
        self._prepared = True
        remove_stale_socket(self.path)
        return self.path

    def cleanup(self) -> None:
        """После остановки воркера: мёртвый сокет и временная папка удаляются."""
        if not self._prepared:
            return
        self._prepared = False
        remove_stale_socket(self.path)
        if self._temp_dir is not None:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None
            self._checked = False
            self.path = self.default

    def _choose_private_dir(self) -> None:
        reason = path_problem(self.path)
        if reason is None:
            try:
                ensure_private_dir(self.path.parent)
                return
            except UnsafeSocketDirError as e:
                reason = str(e)
        base = "/tmp" if os.path.isdir("/tmp") else None
        self._temp_dir = Path(tempfile.mkdtemp(prefix="synex-", dir=base))
        logger.warning(
            "Cannot use the default worker socket directory, using a temporary one",
            reason=reason,
            path=str(self._temp_dir / SOCKET_FILE_NAME),
        )
        self.path = self._temp_dir / SOCKET_FILE_NAME

    def _check_explicit(self) -> None:
        with contextlib.suppress(OSError):
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        problem = path_problem(self.path)
        if problem is not None:
            logger.error(
                "RUST_IPC_SOCKET_PATH is too long; the worker will fall back to TCP. "
                "Use a shorter path or leave it empty",
                path=str(self.path),
                problem=problem,
            )
        with contextlib.suppress(OSError):
            if os.stat(self.path.parent).st_mode & stat.S_IWOTH:
                logger.warning(
                    "RUST_IPC_SOCKET_PATH is in a directory every user can write to. "
                    "Leave it empty to use a private directory",
                    path=str(self.path),
                )
