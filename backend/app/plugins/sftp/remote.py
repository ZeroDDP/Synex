"""Файлы на сервере через SFTP Rust-воркера (действия sftp_*).

Воркер умеет: список папки, сведения об одном пути (sftp_stat, с 2026-11-14), создать и
удалить папку, удалить, переместить и скопировать файл, загрузить и скачать файл
целиком. Остальное собирается здесь:

* домашняя папка: `pwd` служебной командой (служебные команды идут из неё), пути в
  ответах всегда абсолютные;
* сведения о файле: sftp_stat; у сборок до 2026-11-14 его нет, тогда строка из списка
  папки файла (что умеет сборка, запоминается на сессию);
* записи списка: размер, права, время изменения, владелец (uid/gid) и куда ведёт
  ссылка (у старых сборок только размер и права);
* ссылка на папку или нет: пробный список, не больше LINK_PROBES ссылок на папку списка;
* ошибки воркера: код в начале текста (sftp_not_found, sftp_permission_denied,
  sftp_exists, sftp_not_a_directory, sftp_unavailable, sftp_failed; с 2026-11-14) или,
  у старых сборок, тексты статусов SFTP ("No such file", "Permission denied",
  "Failure"). То, что воркер не различает (папка не пуста), backend проверяет до
  операции;
* перемещение: место назначения проверяется заранее, а занятое при overwrite
  освобождается (SFTP-переименование не заменяет файл);
* копирование папки: `cp -R` служебной командой (воркер копирует только файлы), и после
  копирования результат проверяется: воркер теряет код возврата `cp`;
* контрольная сумма файла: `sha256sum`, `shasum -a 256` или `openssl` на сервере, если
  что-то из них есть.
"""

from __future__ import annotations

import contextlib
import json
import re
import shlex
import stat
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal

import structlog

from app.core.config import settings
from app.core.errors import AppError, WorkerCommandError, WorkerTimeoutError
from app.plugins.sftp import paths
from app.plugins.sftp.errors import (
    SftpBadPathError,
    SftpCommandError,
    SftpDirectoryNotEmptyError,
    SftpIsADirectoryError,
    SftpLocalError,
    SftpNotADirectoryError,
    SftpNotFoundError,
    SftpPermissionError,
    SftpProtectedPathError,
    SftpTargetExistsError,
    SftpTimeoutError,
    SftpUnavailableError,
)
from app.plugins.ssh.manager import SSHSessionManager
from app.plugins.ssh.session import CompanionSession

logger = structlog.get_logger()

EntryType = Literal["file", "dir", "link", "other"]
TargetType = Literal["file", "dir"]

LINK_PROBES = 32  # сколько ссылок одного списка проверять «ведёт ли на папку»
_SHA256_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")
_TYPES: dict[str, EntryType] = {"folder": "dir", "file": "file", "symlink": "link"}
_TYPE_CHARS = {"dir": "d", "link": "l", "file": "-", "other": "?"}


@dataclass(frozen=True)
class Entry:
    name: str
    path: str
    type: EntryType
    size: int | None = None
    mode: int | None = None  # биты прав (0o644), без типа файла
    target_type: TargetType | None = None  # куда ведёт ссылка: на папку или нет
    # Время изменения, владелец и цель ссылки: с воркера 2026-11-14, у старых сборок None
    modified_at: datetime | None = None
    uid: int | None = None
    gid: int | None = None
    link_target: str | None = None

    @property
    def is_dir(self) -> bool:
        return self.type == "dir" or self.target_type == "dir"

    @property
    def hidden(self) -> bool:
        return self.name.startswith(".")

    @property
    def permissions(self) -> str | None:
        if self.mode is None:
            return None
        return _TYPE_CHARS[self.type] + stat.filemode(self.mode)[1:]


@dataclass(frozen=True)
class Listing:
    path: str
    parent: str | None
    home: str
    entries: list[Entry]


@dataclass
class ServerInfo:
    home: str
    user: str
    checksum: bool | None = None  # есть ли на сервере sha256 (None: ещё не проверяли)


@dataclass(frozen=True)
class UploadPlan:
    """Куда ляжет загружаемый файл. Данные идут во временную папку рядом с целевым
    файлом (в ней один файл, поэтому размер при опросе прогресса узнаётся дёшево) и
    переносятся на место только после проверки."""

    session_id: str
    target: str
    temp_dir: str
    temp_file: str
    replace: bool  # целевой файл есть, и его разрешили заменить


@dataclass
class _SessionCache:
    info: ServerInfo | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    # SFTP на сервере нет: каждая попытка ждала бы таймаут воркера (около 10 с), поэтому
    # до новой сессии отвечаем сразу той же ошибкой
    unavailable: SftpUnavailableError | None = None


class RemoteFS:
    def __init__(self, manager: SSHSessionManager) -> None:
        self.manager = manager
        self._cache: dict[str, _SessionCache] = {}

    # ---------------------------------------------------------------- server

    async def info(self, session_id: str) -> ServerInfo:
        cached = self._cache.setdefault(session_id, _SessionCache())
        if cached.info is not None:
            return cached.info
        session = self.manager.get(session_id)
        # Служебная команда выполняется из домашней папки: pwd это она
        result = await self.manager.run_service(
            session_id, "pwd", timeout=settings.SFTP_TIMEOUT, name="sftp"
        )
        home = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        if result.exit_code != 0 or not home.startswith("/"):
            raise SftpCommandError(
                "Cannot find the home directory on the server: "
                f"{(result.stderr or result.stdout).strip() or 'no answer'}"
            )
        cached.info = ServerInfo(home=paths.normalize(home, "/"), user=session.username)
        self._prune()
        return cached.info

    async def normalize(self, session_id: str, path: str | None) -> str:
        return paths.normalize(path, (await self.info(session_id)).home)

    # --------------------------------------------------------------- listing

    async def list(self, session_id: str, path: str | None) -> Listing:
        home = (await self.info(session_id)).home
        target = paths.normalize(path, home)
        try:
            entries = await self._list(session_id, target)
        except SftpNotADirectoryError:
            raise SftpNotADirectoryError(f"{target} is not a directory") from None
        except SftpNotFoundError:
            entry = await self._find(session_id, target)
            if entry is not None and not entry.is_dir:
                raise SftpNotADirectoryError(f"{target} is not a directory") from None
            raise SftpNotFoundError(f"Directory {target} not found") from None
        entries = await self._probe_links(session_id, entries)
        entries.sort(key=lambda e: (not e.is_dir, e.name.casefold(), e.name))
        return Listing(path=target, parent=paths.parent(target), home=home, entries=entries)

    async def stat(self, session_id: str, path: str | None) -> Entry:
        target = await self.normalize(session_id, path)
        entry = await self._find(session_id, target)
        if entry is None:
            raise SftpNotFoundError(f"{target} not found")
        if entry.type == "link":
            (entry,) = await self._probe_links(session_id, [entry])
        return entry

    # ------------------------------------------------------------ operations

    async def mkdir(self, session_id: str, path: str, *, parents: bool) -> Entry:
        target = await self.normalize(session_id, path)
        existing = await self._find(session_id, target)
        if existing is not None:
            raise SftpTargetExistsError(f"{target} already exists")
        missing = [target]
        cursor = paths.parent(target)
        while cursor is not None:
            entry = await self._find(session_id, cursor)
            if entry is not None:
                if not entry.is_dir:
                    raise SftpNotADirectoryError(f"{cursor} is not a directory")
                break
            if not parents:
                raise SftpNotFoundError(
                    f"Directory {cursor} does not exist (pass parents=true to create it)"
                )
            missing.append(cursor)
            cursor = paths.parent(cursor)
        for directory in reversed(missing):
            await self._call(session_id, "sftp_create_dir", {"remote_path": directory})
        return await self.stat(session_id, target)

    async def remove(self, session_id: str, path: str, *, recursive: bool) -> str:
        info = await self.info(session_id)
        target = paths.normalize(path, info.home)
        if paths.is_protected(target, info.home):
            raise SftpProtectedPathError(f"{target} cannot be removed")
        entry = await self._find(session_id, target)
        if entry is None:
            raise SftpNotFoundError(f"{target} not found")
        if entry.type == "dir":
            if recursive:
                await self._remove_tree(session_id, target)
                return target
            if await self._list(session_id, target):
                raise SftpDirectoryNotEmptyError(
                    f"Directory {target} is not empty (pass recursive=true to remove it)"
                )
            await self._call(session_id, "sftp_remove_dir", {"remote_path": target})
        else:
            # Ссылка удаляется сама, то, на что она ведёт, остаётся
            await self._call(session_id, "sftp_remove_file", {"remote_path": target})
        return target

    async def move(self, session_id: str, source: str, target: str, *, overwrite: bool) -> Entry:
        info = await self.info(session_id)
        src, dst = paths.normalize(source, info.home), paths.normalize(target, info.home)
        if paths.is_protected(src, info.home):
            raise SftpProtectedPathError(f"{src} cannot be moved")
        entry = await self._prepare_target(session_id, src, dst, overwrite=overwrite)
        if entry.type == "dir" and paths.is_inside(dst, src):
            raise SftpBadPathError(f"Cannot move {src} into itself")
        await self._free_target(session_id, dst, overwrite=overwrite)
        try:
            await self._call(
                session_id, "sftp_move_file", {"remote_path": src, "destination_path": dst}
            )
        except SftpCommandError as e:
            raise SftpCommandError(f"The server refused to move {src} to {dst}{e.reason}") from e
        return await self.stat(session_id, dst)

    async def copy(self, session_id: str, source: str, target: str, *, overwrite: bool) -> Entry:
        info = await self.info(session_id)
        src, dst = paths.normalize(source, info.home), paths.normalize(target, info.home)
        entry = await self._prepare_target(session_id, src, dst, overwrite=overwrite)
        if entry.is_dir and paths.is_inside(dst, src):
            raise SftpBadPathError(f"Cannot copy {src} into itself")
        await self._free_target(session_id, dst, overwrite=overwrite)
        if entry.type == "dir":
            command = f"cp -R -- {shlex.quote(src)} {shlex.quote(dst)}"
            result = await self.manager.run_service(
                session_id, command, timeout=settings.SFTP_LONG_TIMEOUT, name="sftp"
            )
            if result.timed_out:
                raise SftpTimeoutError(f"Copying {src} took longer than SFTP_LONG_TIMEOUT")
            if result.exit_code != 0:
                raise SftpCommandError(
                    f"Cannot copy {src}: {(result.stderr or result.stdout).strip()}"
                )
        else:
            await self._call(
                session_id,
                "sftp_copy_file",
                {"remote_path": src, "destination_path": dst},
                long=True,
            )
        copied = await self._find(session_id, dst)
        if copied is None:  # воркер потерял код возврата cp: проверяем результат сами
            raise SftpCommandError(f"The server did not copy {src} to {dst}")
        return await self.stat(session_id, dst)

    # -------------------------------------------------------------- transfers

    async def plan_upload(
        self, session_id: str, remote_path: str, local_name: str, transfer_id: str,
        *, overwrite: bool,
    ) -> UploadPlan:  # fmt: skip
        """Куда ляжет файл с этого компьютера. Папка (или путь с "/" в конце) значит
        «в неё под тем же именем»."""
        home = (await self.info(session_id)).home
        target = paths.normalize(remote_path, home)
        existing = await self._find(session_id, target)
        if remote_path.endswith("/") or (existing is not None and existing.is_dir):
            if existing is None:
                raise SftpNotFoundError(f"Directory {target} not found")
            target = paths.join(target, local_name)
            existing = await self._find(session_id, target)
        if existing is not None:
            if existing.is_dir:
                raise SftpIsADirectoryError(f"{target} is a directory")
            if not overwrite:
                raise SftpTargetExistsError(
                    f"{target} already exists (pass overwrite=true to replace it)"
                )
        await self._require_dir(session_id, paths.parent(target) or "/")
        temp_dir = paths.join(paths.parent(target) or "/", f".synex-{transfer_id}.part")
        return UploadPlan(
            session_id=session_id,
            target=target,
            temp_dir=temp_dir,
            temp_file=paths.join(temp_dir, paths.name(target)),
            replace=existing is not None,
        )

    async def begin_upload(self, plan: UploadPlan) -> None:
        await self._call(plan.session_id, "sftp_create_dir", {"remote_path": plan.temp_dir})

    async def upload(
        self,
        plan: UploadPlan,
        local_path: str,
        companion: CompanionSession | None,
    ) -> None:
        """Сам файл: в служебной сессии (основная свободна) или, если её нет, в основной."""
        payload = {"local_path": local_path, "remote_path": plan.temp_file}
        await self._transfer(plan.session_id, companion, "sftp_upload_file", payload)

    async def commit_upload(self, plan: UploadPlan) -> Entry:
        """Новый файл на место целевого. SFTP-переименование не заменяет файл, поэтому
        старый сначала уходит во временную папку и удаляется, только когда новый уже на
        месте; если поставить новый не вышло, старый возвращается."""
        previous: str | None = None
        if plan.replace:
            current = await self._find(plan.session_id, plan.target)
            if current is not None:
                if current.is_dir:
                    raise SftpIsADirectoryError(f"{plan.target} became a directory")
                previous = paths.join(plan.temp_dir, paths.name(plan.target) + ".previous")
                await self._move(plan.session_id, plan.target, previous)
        elif await self._find(plan.session_id, plan.target) is not None:
            raise SftpTargetExistsError(f"{plan.target} appeared during the transfer")
        try:
            await self._move(plan.session_id, plan.temp_file, plan.target)
        except AppError as e:
            if previous is not None:
                with contextlib.suppress(AppError):
                    await self._move(plan.session_id, previous, plan.target)
            reason = e.reason if isinstance(e, SftpCommandError) else f": {e.detail}"
            raise SftpCommandError(f"Cannot put the file to {plan.target}{reason}") from e
        if previous is not None:
            await self._call(plan.session_id, "sftp_remove_file", {"remote_path": previous})
        await self._call(plan.session_id, "sftp_remove_dir", {"remote_path": plan.temp_dir})
        return await self.stat(plan.session_id, plan.target)

    async def _move(self, session_id: str, source: str, target: str) -> None:
        await self._call(
            session_id, "sftp_move_file", {"remote_path": source, "destination_path": target}
        )

    async def abort_upload(self, plan: UploadPlan) -> None:
        """Убрать временную папку. Ошибки не важны: уборка по возможности. Если в ней
        остался прежний файл (вернуть его на место не удалось), папка остаётся вместе с
        ним: удалять чужие данные нельзя."""
        for action, path in (
            ("sftp_remove_file", plan.temp_file),
            ("sftp_remove_dir", plan.temp_dir),
        ):
            try:
                await self._call(plan.session_id, action, {"remote_path": path}, wait=30)
            except AppError:
                pass

    async def temp_size(self, plan: UploadPlan) -> int | None:
        """Сколько уже записано на сервер. None, если сессия занята (не ждём)."""
        try:
            if self._stat_supported(plan.session_id) is not False:
                entry = await self._stat(plan.session_id, plan.temp_file, wait=0)
                return entry.size if entry is not None else None
            entries = await self._list(plan.session_id, plan.temp_dir, wait=0)
        except AppError:
            return None
        return next((e.size for e in entries if e.path == plan.temp_file), None)

    async def remote_file(self, session_id: str, remote_path: str) -> Entry:
        """Файл, который будут скачивать: он есть и это файл (не папка)."""
        entry = await self.stat(session_id, remote_path)
        if entry.is_dir:
            raise SftpIsADirectoryError(f"{entry.path} is a directory")
        if entry.type == "other":
            raise SftpCommandError(f"{entry.path} is not a regular file")
        return entry

    async def download(
        self, session_id: str, remote_path: str, local_path: str,
        companion: CompanionSession | None,
    ) -> None:  # fmt: skip
        payload = {"remote_path": remote_path, "local_path": local_path}
        await self._transfer(session_id, companion, "sftp_download_file", payload)

    async def file_size(self, session_id: str, path: str) -> int | None:
        entry = await self._find(session_id, path)
        return entry.size if entry is not None else None

    async def checksum(self, session_id: str, path: str) -> str | None:
        """SHA-256 файла на сервере; None, если на сервере нечем посчитать."""
        info = await self.info(session_id)
        if info.checksum is False:
            return None
        quoted = shlex.quote(path)
        command = (
            f"sha256sum {quoted} 2>/dev/null || shasum -a 256 {quoted} 2>/dev/null "
            f"|| openssl dgst -sha256 -r {quoted}"
        )
        result = await self.manager.run_service(
            session_id, command, timeout=settings.SFTP_LONG_TIMEOUT, name="sftp"
        )
        found = _SHA256_RE.search(result.stdout)
        if found is None:
            if result.timed_out:
                raise SftpTimeoutError(f"Checksum of {path} took longer than SFTP_LONG_TIMEOUT")
            info.checksum = False
            logger.info("No sha256 tool on the server: transfers check only the size")
            return None
        info.checksum = True
        return found.group(1).lower()

    def forget(self, session_id: str) -> None:
        self._cache.pop(session_id, None)

    # --------------------------------------------------------------- helpers

    async def _list(self, session_id: str, path: str, *, wait: float | None = None) -> list[Entry]:
        payload = await self._call(session_id, "sftp_list_dir", {"remote_path": path}, wait=wait)
        try:
            raw = json.loads(str(payload.get("stdout") or "[]"))
        except ValueError:
            raise SftpCommandError(f"The worker returned an unreadable listing of {path}") from None
        entries = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name or name in (".", ".."):
                continue
            entries.append(_entry(item, name, paths.join(path, name)))
        return entries

    async def _find(self, session_id: str, path: str) -> Entry | None:
        """Сведения о path (sftp_stat или строка из списка его папки); None, если его нет."""
        directory = paths.parent(path)
        if directory is None:
            return Entry(name="/", path="/", type="dir")
        if self._stat_supported(session_id) is not False:
            try:
                return await self._stat(session_id, path)
            except _NoStatError:
                pass  # сборка воркера до 2026-11-14: дальше через список папки
        try:
            entries = await self._list(session_id, directory)
        except (SftpNotFoundError, SftpNotADirectoryError):
            return None
        return next((e for e in entries if e.name == paths.name(path)), None)

    async def _stat(self, session_id: str, path: str, *, wait: float | None = None) -> Entry | None:
        """sftp_stat (без перехода по ссылке). None, если пути нет; _NoStatError, если сборка
        воркера sftp_stat не знает."""
        try:
            payload = await self._call(session_id, "sftp_stat", {"remote_path": path}, wait=wait)
        except SftpUnavailableError as e:
            if "Unknown action" not in e.detail:
                raise
            self._cache.setdefault(session_id, _SessionCache()).unavailable = None
            self._cache[session_id].extra["stat"] = False
            raise _NoStatError from None
        except (SftpNotFoundError, SftpNotADirectoryError):
            return None
        self._cache.setdefault(session_id, _SessionCache()).extra["stat"] = True
        try:
            item = json.loads(str(payload.get("stdout") or "{}"))
        except ValueError:
            raise SftpCommandError(f"The worker returned unreadable details of {path}") from None
        if not isinstance(item, dict):
            raise SftpCommandError(f"The worker returned unreadable details of {path}")
        return _entry(item, paths.name(path) or "/", path)

    def _stat_supported(self, session_id: str) -> bool | None:
        cached = self._cache.get(session_id)
        return None if cached is None else cached.extra.get("stat")

    async def _require_dir(self, session_id: str, path: str) -> None:
        entry = await self._find(session_id, path)
        if entry is None:
            raise SftpNotFoundError(f"Directory {path} does not exist")
        if not entry.is_dir and entry.type != "link":
            raise SftpNotADirectoryError(f"{path} is not a directory")

    async def _prepare_target(
        self, session_id: str, src: str, dst: str, *, overwrite: bool
    ) -> Entry:
        if src == dst:
            raise SftpBadPathError("Source and target are the same")
        entry = await self._find(session_id, src)
        if entry is None:
            raise SftpNotFoundError(f"{src} not found")
        await self._require_dir(session_id, paths.parent(dst) or "/")
        existing = await self._find(session_id, dst)
        if existing is not None:
            if existing.type == "dir":
                raise SftpIsADirectoryError(f"{dst} is a directory and cannot be replaced")
            if not overwrite:
                raise SftpTargetExistsError(
                    f"{dst} already exists (pass overwrite=true to replace it)"
                )
        return entry

    async def _free_target(self, session_id: str, dst: str, *, overwrite: bool) -> None:
        if overwrite and await self._find(session_id, dst) is not None:
            await self._call(session_id, "sftp_remove_file", {"remote_path": dst})

    async def _remove_tree(self, session_id: str, directory: str) -> None:
        for entry in await self._list(session_id, directory):
            if entry.type == "dir":
                await self._remove_tree(session_id, entry.path)
            else:
                await self._call(session_id, "sftp_remove_file", {"remote_path": entry.path})
        await self._call(session_id, "sftp_remove_dir", {"remote_path": directory})

    async def _probe_links(self, session_id: str, entries: list[Entry]) -> list[Entry]:
        result, probes = [], 0
        for entry in entries:
            if entry.type == "link" and probes < LINK_PROBES:
                probes += 1
                try:
                    await self._call(session_id, "sftp_list_dir", {"remote_path": entry.path})
                except SftpNotFoundError:
                    pass  # ведёт на файл, или ссылка битая: SFTP воркера их не различает
                except AppError:
                    pass
                else:
                    entry = replace(entry, target_type="dir")
            result.append(entry)
        return result

    async def _call(
        self,
        session_id: str,
        action: str,
        payload: dict[str, Any],
        *,
        long: bool = False,
        wait: float | None = None,
    ) -> dict[str, Any]:
        timeout = settings.SFTP_LONG_TIMEOUT if long else settings.SFTP_TIMEOUT
        self._check_available(session_id)
        try:
            return await self.manager.request(
                session_id, action, payload, timeout=timeout, wait=wait, name="sftp"
            )
        except WorkerCommandError as e:
            raise self._remember(session_id, _translate(e.stderr, payload)) from e
        except WorkerTimeoutError as e:
            raise SftpTimeoutError(f"The worker did not finish {action} within {timeout:g}s") from e

    async def _transfer(
        self,
        session_id: str,
        companion: CompanionSession | None,
        action: str,
        payload: dict[str, Any],
    ) -> None:
        timeout = settings.SFTP_TRANSFER_TIMEOUT
        self._check_available(session_id)
        try:
            if companion is not None:
                await self.manager.companion_request(companion, action, payload, timeout=timeout)
            else:
                await self.manager.request(
                    session_id, action, payload, timeout=timeout,
                    wait=settings.SFTP_SESSION_WAIT, name="sftp",
                )  # fmt: skip
        except WorkerCommandError as e:
            raise self._remember(session_id, _translate(e.stderr, payload)) from e
        except WorkerTimeoutError as e:
            raise SftpTimeoutError(
                f"The transfer did not finish within {timeout:g}s (SFTP_TRANSFER_TIMEOUT)"
            ) from e

    def _check_available(self, session_id: str) -> None:
        cached = self._cache.get(session_id)
        if cached is not None and cached.unavailable is not None:
            raise SftpUnavailableError(cached.unavailable.detail)

    def _remember(self, session_id: str, error: AppError) -> AppError:
        if isinstance(error, SftpUnavailableError):
            self._cache.setdefault(session_id, _SessionCache()).unavailable = error
        return error

    def _prune(self) -> None:
        alive = {s.id for s in self.manager.sessions()}
        for session_id in list(self._cache):
            if session_id not in alive:
                del self._cache[session_id]


class _NoStatError(Exception):
    """Сборка воркера не знает sftp_stat."""


# Коды ошибок воркера (с 2026-11-14) в начале stderr: "sftp_not_found: ..."
_CODE_RE = re.compile(r"^(sftp_[a-z_]+):\s*")


def _translate(stderr: str, payload: dict[str, Any]) -> AppError:
    """Ошибка воркера → ошибка API. С 2026-11-14 воркер ставит код в начало:
    "sftp_not_found: Failed to ...: No such file"; старые сборки писали только текст:
    "SSH internal error: Failed to ...: <статус SFTP>" (No such file, Permission denied,
    Failure)."""
    message = stderr.removeprefix("SSH internal error: ").strip()
    code = ""
    found = _CODE_RE.match(message)
    if found is not None:
        code, message = found.group(1), message[found.end() :]
    lower = message.lower()
    where = payload.get("remote_path") or payload.get("local_path") or ""
    if "failed to open local file" in lower or "failed to create local file" in lower:
        return SftpLocalError(f"This computer: {message}")
    if code == "sftp_not_found":
        return SftpNotFoundError(f"{where} not found on the server ({message})")
    if code == "sftp_permission_denied":
        return SftpPermissionError(f"No permission on the server for {where} ({message})")
    if code == "sftp_exists":
        target = payload.get("destination_path") or where
        return SftpTargetExistsError(f"{target} already exists on the server ({message})")
    if code == "sftp_not_a_directory":
        return SftpNotADirectoryError(f"{where} is not a directory ({message})")
    if code == "sftp_unavailable":
        return SftpUnavailableError(
            f"The SSH server does not offer SFTP ({message}). On the server, "
            "/etc/ssh/sshd_config needs a line like 'Subsystem sftp internal-sftp' (then "
            "reload sshd and open a new session)"
        )
    if lower.startswith("unknown action"):
        # Старая сборка synex_worker (например, .exe до SFTP) не знает действий sftp_*
        return SftpUnavailableError(
            f"This synex_worker build has no SFTP ({message}). Update the worker: build "
            "core/ with 'cargo build --release' or take a newer binary, then restart the backend"
        )
    if "failed to initialize sftp" in lower or "subsystem" in lower:
        return SftpUnavailableError(
            f"The SSH server does not offer SFTP ({message}). On the server, "
            "/etc/ssh/sshd_config needs a line like 'Subsystem sftp internal-sftp' (then "
            "reload sshd and open a new session)"
        )
    if "no such file" in lower:
        return SftpNotFoundError(f"{where} not found on the server ({message})")
    if "permission denied" in lower:
        return SftpPermissionError(f"No permission on the server for {where} ({message})")
    if "no space" in lower or "quota" in lower:
        return SftpCommandError(f"No space left on the server: {message}")
    error = SftpCommandError(f"The server refused the operation: {message or 'no reason given'}")
    error.reason = f": {message}" if message else ""
    return error


def _entry(item: dict[str, Any], name: str, path: str) -> Entry:
    """Запись из ответа воркера: size (до 2026-11-14 weight), rights, mtime, uid, gid,
    link_target."""
    size = item.get("size", item.get("weight"))
    mtime = _as_int(item.get("mtime"))
    link = item.get("link_target")
    return Entry(
        name=name,
        path=path,
        type=_TYPES.get(str(item.get("Type")), "other"),
        size=_as_int(size),
        mode=_mode(str(item.get("rights") or "")),
        modified_at=datetime.fromtimestamp(mtime, UTC) if mtime is not None else None,
        uid=_as_int(item.get("uid")),
        gid=_as_int(item.get("gid")),
        link_target=link if isinstance(link, str) and link else None,
    )


def _mode(rights: str) -> int | None:
    if len(rights) != 9:
        return None
    mode = 0
    for index, char in enumerate(rights):
        if char not in "-":
            mode |= 1 << (8 - index)
    return mode


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
