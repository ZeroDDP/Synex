"""Процесс воркера на Linux и macOS: не оставлять его сиротой.

На Windows это делает Job Object (manager.py). На Linux и macOS:

* Своя сессия (start_new_session). Ctrl+C в терминале шлёт SIGINT всей группе
  процессов: без этого воркер умирал бы раньше, чем backend успеет закрыть SSH-сессии,
  а супервизор успевал бы его «перезапустить».
* Linux: PR_SET_PDEATHSIG. Если backend убит (kill -9, падение, закрытие IDE), ядро
  пошлёт воркеру SIGTERM. Сигнал привязан к потоку, который запустил процесс: воркер
  запускается из потока event loop, который живёт столько же, сколько backend.
* macOS такого механизма не имеет. Поэтому PID воркера записывается в файл рядом с
  сокетом (worker.sock.pid). При следующем запуске backend находит воркер, оставшийся
  от прошлого раза, и останавливает его: иначе тот держал бы сокет и порт. Процесс
  останавливается, только если его backend уже не работает и это тот же процесс:
  совпадает время запуска (защита от повторно использованного PID).
* stdin воркера это пайп, который backend держит открытым. Когда backend завершается
  любым способом, ОС закрывает пайп, и воркер видит EOF. Сейчас воркер stdin не читает;
  если научится завершаться по EOF (вопрос Rust-стороне), это заработает и на macOS.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

PR_SET_PDEATHSIG = 1


def linux_pdeathsig() -> Callable[[], None] | None:
    """preexec_fn для Popen: SIGTERM воркеру, когда умрёт backend (только Linux)."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        prctl = ctypes.CDLL(None, use_errno=True).prctl  # libc загружаем до fork
    except (OSError, AttributeError):
        return None
    parent = os.getpid()

    def set_pdeathsig() -> None:
        # Выполняется в дочернем процессе между fork и exec: только простые вызовы
        prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
        if os.getppid() != parent:  # backend умер раньше, чем мы успели подписаться
            os.kill(os.getpid(), signal.SIGTERM)

    return set_pdeathsig


def popen_options() -> dict[str, Any]:
    """Платформенные параметры subprocess.Popen для воркера."""
    if sys.platform == "win32":
        import subprocess

        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    options: dict[str, Any] = {"start_new_session": True}
    preexec = linux_pdeathsig()
    if preexec is not None:
        options["preexec_fn"] = preexec
    return options


# ------------------------------------------------------------------ processes


def is_alive(pid: int) -> bool:
    """Процесс существует и не зомби."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # чужой процесс: существует, но не наш
    with contextlib.suppress(OSError):
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    return True


def process_executable(pid: int) -> str | None:
    """Путь к бинарнику процесса (None: узнать не удалось)."""
    if sys.platform.startswith("linux"):
        try:
            path = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            return None
        return path.removesuffix(" (deleted)")  # бинарник пересобрали, пока он работал
    if sys.platform == "darwin":
        return _macos_proc_pidpath(pid) or _ps_command(pid)
    return None


def _macos_proc_pidpath(pid: int) -> str | None:
    for library in ("/usr/lib/libproc.dylib", ctypes.util.find_library("proc"), None):
        try:
            proc_pidpath = ctypes.CDLL(library).proc_pidpath
        except (OSError, AttributeError, TypeError):
            continue
        buffer = ctypes.create_string_buffer(4096)
        if proc_pidpath(ctypes.c_int(pid), buffer, ctypes.c_uint32(len(buffer))) > 0:
            return os.fsdecode(buffer.value)
        return None
    return None


def _ps_command(pid: int) -> str | None:
    """Запасной путь: имя команды из ps (на macOS это путь, с которым её запустили)."""
    try:
        out = subprocess.run(
            ["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def process_start_time(pid: int) -> str | None:
    """Когда процесс запущен (None: узнать не удалось). Пара (PID, время запуска)
    однозначно задаёт процесс: повторно выданный PID будет с другим временем. exec
    время запуска не меняет (Python из python.org на macOS перезапускает себя в
    Python.app, и путь к бинарнику у процесса уже другой)."""
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return None
        fields = stat.rsplit(")", 1)[1].split()
        return fields[19] if len(fields) > 19 else None  # starttime, такты с загрузки
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return " ".join(out.stdout.split()) or None
    return None


def same_file(a: str, b: str) -> bool:
    """Один и тот же файл. По inode, а не по строке: на macOS путь процесса бывает
    вида /System/Volumes/Data/Users/..., а записанный — /Users/..."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.realpath(a) == os.path.realpath(b)


def stop_process(pid: int, timeout: float = 3.0) -> bool:
    """SIGTERM, затем SIGKILL. True: процесс завершился."""
    for sig, wait in ((signal.SIGTERM, timeout), (signal.SIGKILL, 2.0)):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, sig)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not is_alive(pid):
                return True
            time.sleep(0.05)
    return not is_alive(pid)


# -------------------------------------------------------------------- pid file


class WorkerPidFile:
    """worker.sock.pid: {worker_pid, backend_pid, executable, started} запущенного воркера."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, worker_pid: int, executable: str) -> None:
        data = {
            "worker_pid": worker_pid,
            "backend_pid": os.getpid(),
            "executable": os.path.realpath(executable),
            "started": process_start_time(worker_pid),
        }
        try:
            tmp = self.path.with_name(self.path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning("Cannot write worker PID file", path=str(self.path), error=repr(e))

    def read(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.path.read_text())
            int(data["worker_pid"]), int(data["backend_pid"]), str(data["executable"])
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("Ignoring a broken worker PID file", path=str(self.path))
            self._remove()
            return None
        return data

    def clear(self, worker_pid: int) -> None:
        """Удаляет файл, если он про этот воркер (другой backend мог его перезаписать)."""
        data = self.read()
        if data is not None and data["worker_pid"] == worker_pid:
            self._remove()

    def stop_orphan(self) -> None:
        """Останавливает воркер, оставшийся от прошлого запуска backend."""
        data = self.read()
        if data is None:
            return
        worker_pid, backend_pid = int(data["worker_pid"]), int(data["backend_pid"])
        if backend_pid != os.getpid() and is_alive(backend_pid):
            logger.warning(
                "Another backend seems to be running with its own worker; leaving it alone",
                backend_pid=backend_pid,
                worker_pid=worker_pid,
            )
            return
        if not is_alive(worker_pid):
            self._remove()
            return
        if not self._same_process(worker_pid, data):
            self._remove()  # PID уже занят другим процессом
            return
        stopped = stop_process(worker_pid)
        logger.warning(
            "Stopped a Rust worker left from a previous backend run",
            pid=worker_pid,
            stopped=stopped,
        )
        if stopped:
            self._remove()

    @staticmethod
    def _same_process(pid: int, data: dict[str, Any]) -> bool:
        """Тот ли это процесс: по времени запуска, а без него (старый файл) по бинарнику."""
        recorded = data.get("started")
        started = process_start_time(pid)
        if recorded and started:
            if started == recorded:
                return True
            logger.info(
                "Worker PID now belongs to another process, leaving it alone",
                pid=pid,
                recorded_start=recorded,
                start=started,
            )
            return False
        executable = process_executable(pid)
        if executable is not None and same_file(executable, data["executable"]):
            return True
        logger.info(
            "Worker PID file points to another program, leaving it alone",
            pid=pid,
            expected=data["executable"],
            found=executable,
        )
        return False

    def _remove(self) -> None:
        with contextlib.suppress(OSError):
            self.path.unlink()
