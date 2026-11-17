"""Передача папки целиком: одна передача (Transfer) на всю папку.

Воркер передаёт только файлы, поэтому папку backend обходит сам и передаёт по одному
файлу в одной служебной сессии. Порядок:

* обход (status `scanning`): сколько файлов и байт впереди, что пропускается (tree.py);
* новая папка собирается во временной рядом с целевой (`.synex-<id>.part/` на сервере,
  `<имя>.<id>.synex-part` на этом компьютере) и встаёт на место переименованием, когда
  готова целиком. При ошибке и отмене временная папка удаляется: наполовину переданной
  папки на месте не бывает;
* целевая папка уже есть (`overwrite: true`): папки объединяются. Каждый файл идёт под
  временным именем и встаёт на своё место после проверки; чего нет в источнике, не
  трогается. При ошибке и отмене уже переданные файлы остаются;
* отмена закрывает служебную сессию, но воркер действие не прерывает: файл, который шёл
  в эту минуту, он доделывает в фоне и может создать его уже после уборки. Такие файлы
  убираются следом (TransferManager.remove_remote и remove_local);
* проверка: SHA-256 пачкой (одна команда на сервере на несколько десятков файлов) или,
  если на сервере считать нечем, размер. Файл, который не сошёлся, передаётся ещё раз
  (скорее всего, его меняли во время передачи), а если не сошёлся снова, пропускается;
* то, что нельзя передать (нет прав, занято имя, имя не годится этой ОС), пропускается и
  попадает в `skipped` передачи. Передачу целиком останавливают сбои связи и сервера.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from app.core.config import settings
from app.core.errors import AppError
from app.plugins.sftp import local, paths
from app.plugins.sftp.errors import (
    SftpIsADirectoryError,
    SftpLocalError,
    SftpLocalExistsError,
    SftpLocalPermissionError,
    SftpNotFoundError,
    SftpPermissionError,
    SftpTargetExistsError,
)
from app.plugins.sftp.remote import Entry, FolderPlan
from app.plugins.sftp.tree import DirItem, FileItem, Tree

if TYPE_CHECKING:
    from app.plugins.sftp.transfers import Transfer, TransferManager

logger = structlog.get_logger()

# Переданные файлы проверяются и встают на место пачками: не больше стольких файлов и
# байт в пачке (большой файл проверяется сразу)
BATCH_FILES = 64
BATCH_BYTES = 64 * 1024 * 1024
SKIPPED_SHOWN = 50  # столько пропущенных перечисляется в передаче (счётчик полный)
# Сколько раз и с какой паузой (сек) ставить скачанную папку на место, если Windows
# отказывает: файл в ней ещё открыт другой программой
RENAME_TRIES = 10
RENAME_PAUSE = 0.3
HASH_CHUNK = 1024 * 1024

REASON_CHANGED = (
    "The copy did not match the original twice: the file was changing during the transfer"
)
REASON_DIR_ON_SERVER = "A folder with this name is on the server"
REASON_FILE_ON_SERVER = "A file with this name is on the server"
REASON_DIR_HERE = "A folder with this name is on this computer"
REASON_FILE_HERE = "A file with this name is on this computer"
REASON_CASE = "Another name here differs only by letter case, which this computer ignores"


@dataclass
class _Pending:
    """Файл, который передан и ждёт проверки."""

    file: FileItem
    work: Any  # где данные лежат сейчас (str на сервере, Path на этом компьютере)
    final: Any  # где файл должен оказаться
    digest: str  # SHA-256 и размер на этом компьютере
    size: int
    replace: bool = False  # на месте final уже есть файл


class FolderJob:
    def __init__(self, manager: TransferManager, transfer: Transfer) -> None:
        self.m = manager
        self.t = transfer
        self.remote = manager.remote
        self.sid = transfer.session_id
        self.base = 0  # байты файлов, с которыми уже закончили
        self._hashed = 0  # файлы, сверенные по SHA-256
        self._sized = 0  # файлы, у которых сверен только размер
        self._names = 0

    # ----------------------------------------------------------- accounting

    def _account(self, tree: Tree) -> None:
        t = self.t
        t.files_total = tree.files
        t.bytes_total = tree.size
        for skipped in tree.skipped:
            self._note(skipped.path, skipped.reason)

    def _note(self, path: str, reason: str) -> None:
        t = self.t
        t.skipped_count += 1
        if len(t.skipped) < SKIPPED_SHOWN:
            t.skipped.append({"path": path, "reason": reason})

    def _skip(self, file: FileItem, reason: str) -> None:
        """Файл, который не будет передан: его байты не ждём."""
        t = self.t
        t.files_total = max(0, (t.files_total or 0) - 1)
        t.bytes_total = max(self.base, (t.bytes_total or 0) - file.size)
        self._note(file.rel, reason)

    def _sent(self, file: FileItem) -> None:
        """Данные файла переданы (проверка впереди)."""
        self.base += file.size
        self.t.files_done += 1

    def _drop(self, file: FileItem, reason: str) -> None:
        """Файл, который передали, но сохранить не вышло: байты уже посчитаны."""
        t = self.t
        t.files_done = max(0, t.files_done - 1)
        t.files_total = max(0, (t.files_total or 0) - 1)
        self._note(file.rel, reason)

    @staticmethod
    def _full(pending: list[_Pending]) -> bool:
        return len(pending) >= BATCH_FILES or sum(item.size for item in pending) >= BATCH_BYTES

    def _skip_dir(self, item: DirItem, reason: str) -> None:
        files, size = _totals(item)
        t = self.t
        t.files_total = max(0, (t.files_total or 0) - files)
        t.bytes_total = max(self.base, (t.bytes_total or 0) - size)
        self._note(item.rel + "/", reason)

    def _temp_name(self) -> str:
        self._names += 1
        return f".synex-{self.t.id}-{self._names}.part"

    def _finish(self) -> None:
        t = self.t
        t.current = None
        # Итог ровно по переданному: пропущенный посреди передачи файл мог оставить
        # лишние байты в счёте
        t.bytes_done = t.bytes_total = self.base
        if self._hashed and not self._sized:
            t.verified = True

    # ----------------------------------------------------------- verification

    async def _settle(self, pending: list[_Pending]) -> None:
        """Проверить пачку переданных файлов и поставить на место те, что сошлись."""
        if not pending:
            return
        sums = await self._sums([self._remote_side(p) for p in pending])
        for index, item in enumerate(pending):
            matched = await self._matches(item, sums[index] if sums is not None else None)
            if not matched:
                # Не сошлось: ещё одна попытка для этого файла, и проверка его одного
                matched = await self._again(item) and await self._matches(
                    item, (await self._sums([self._remote_side(item)]) or [None])[0]
                )
            if not matched:
                await self._discard(item.work)
                self._drop(item.file, REASON_CHANGED)
                continue
            await self._place(item)
        await self.m._progress(self.t)

    async def _sums(self, files: list[str]) -> list[str | None] | None:
        if not settings.SFTP_VERIFY:
            return None
        return await self.remote.checksums(self.sid, files, self.t.companion)

    async def _matches(self, item: _Pending, remote_sum: str | None) -> bool:
        if remote_sum is not None:
            if remote_sum != item.digest:
                return False
            self._hashed += 1
            return True
        size = await self.remote.size_of(self.sid, self._remote_side(item), self.t.companion)
        if size != item.size:
            return False
        self._sized += 1
        return True

    # Что делает каждая сторона по-своему

    def _remote_side(self, item: _Pending) -> str:
        raise NotImplementedError

    async def _again(self, item: _Pending) -> bool:
        raise NotImplementedError

    async def _place(self, item: _Pending) -> None:
        raise NotImplementedError

    async def _discard(self, work: Any) -> None:
        raise NotImplementedError


class UploadFolder(FolderJob):
    """Папка этого компьютера на сервер."""

    def __init__(
        self, manager: TransferManager, transfer: Transfer, source: Path, plan: FolderPlan
    ) -> None:
        super().__init__(manager, transfer)
        self.source = source
        self.plan = plan
        self._temps: set[str] = set()  # временные файлы на сервере (объединение папок)

    async def run(self) -> None:
        m, t, plan = self.m, self.t, self.plan
        tree = await asyncio.to_thread(
            local.scan_tree,
            self.source,
            limit=settings.SFTP_FOLDER_LIMIT,
            stopped=lambda: t.cancel_requested,
        )
        self._account(tree)
        m._check_cancel(t)
        await m._set_status(t, "running")
        try:
            t.companion = await m._companion(t)
            if plan.temp_dir is not None:
                await self.remote.make_dir(self.sid, plan.temp_dir, t.companion)
                await self.remote.make_dir(self.sid, plan.root, t.companion)
            await self._dir(tree.root, plan.root, listed=plan.merge)
            m._check_cancel(t)
            if plan.temp_dir is not None:
                await m._set_status(t, "verifying")
                await self.remote.rename(self.sid, plan.root, plan.target, t.companion)
                with contextlib.suppress(AppError):  # папка уже на месте
                    await self.remote.delete_dir(self.sid, plan.temp_dir, t.companion)
                plan = self.plan = replace(plan, temp_dir=None)
            await m._close_companion(t)
        except BaseException:
            await m._close_companion(t)
            await m.remove_remote(self.sid, files=list(self._temps), folder=plan.temp_dir)
            raise
        self._finish()

    async def _dir(self, item: DirItem, remote_dir: str, *, listed: bool) -> None:
        """Файлы и папки item в папку сервера remote_dir. listed: она была там до
        передачи, и в ней может лежать что-то с теми же именами."""
        t = self.t
        existing: dict[str, Entry] = {}
        if listed:
            try:
                entries = await self.remote.entries(self.sid, remote_dir, t.companion)
            except SftpPermissionError as e:
                if not item.rel:
                    raise
                self._skip_dir(item, e.detail)
                return
            existing = {entry.name: entry for entry in entries}
        pending: list[_Pending] = []
        for file in item.files:
            self.m._check_cancel(t)
            current = existing.get(file.name)
            final = paths.join(remote_dir, file.name)
            if current is not None and (
                current.type == "dir"
                or (
                    current.type == "link"
                    and await self.remote.is_dir_link(self.sid, final, t.companion)
                )
            ):
                self._skip(file, REASON_DIR_ON_SERVER)
                continue
            # В новой папке (она сама временная) файл сразу под своим именем
            work = paths.join(remote_dir, self._temp_name()) if self.plan.merge else final
            sent = await self._send(file, work)
            if sent is None:
                continue
            pending.append(_Pending(file, work, final, *sent, replace=current is not None))
            if self._full(pending):
                await self._settle(pending)
                pending = []
        await self._settle(pending)
        for sub in item.dirs:
            self.m._check_cancel(t)
            path = paths.join(remote_dir, sub.name)
            current = existing.get(sub.name)
            if current is None:
                try:
                    await self.remote.make_dir(self.sid, path, t.companion)
                except SftpPermissionError as e:
                    self._skip_dir(sub, e.detail)
                    continue
                await self._dir(sub, path, listed=False)
            elif current.type == "dir" or (
                current.type == "link"
                and await self.remote.is_dir_link(self.sid, path, t.companion)
            ):
                await self._dir(sub, path, listed=True)
            else:
                self._skip_dir(sub, REASON_FILE_ON_SERVER)

    def _local(self, file: FileItem) -> Path:
        return self.source.joinpath(*file.rel.split("/"))

    async def _send(self, file: FileItem, work: str) -> tuple[str, int] | None:
        """Файл на сервер. (SHA-256, размер) локального файла или None, если пропущен."""
        t = self.t
        t.current = file.rel
        source = self._local(file)
        hashing = _hash_in_thread(source)
        if self.plan.merge:
            self._temps.add(work)
        try:
            await self.m._with_progress(
                t,
                self.remote.upload_to(self.sid, str(source), work, t.companion),
                lambda: self._uploaded(work),
                done_after=self.base + file.size,
            )
        except (SftpLocalError, SftpPermissionError) as e:
            await self._discard(work)
            self._skip(file, e.detail)
            return None
        self._sent(file)
        try:
            return await hashing
        except OSError as e:
            await self._discard(work)
            self._drop(file, f"Cannot read the file on this computer: {e.strerror or e}")
            return None

    async def _uploaded(self, work: str) -> int | None:
        size = await self.remote.size_now(self.sid, work)
        return None if size is None else self.base + size

    def _remote_side(self, item: _Pending) -> str:
        return item.work

    async def _again(self, item: _Pending) -> bool:
        source = self._local(item.file)
        hashing = _hash_in_thread(source)
        try:
            await self.remote.upload_to(self.sid, str(source), item.work, self.t.companion)
        except (SftpLocalError, SftpPermissionError):
            return False
        try:
            item.digest, item.size = await hashing
        except OSError:
            return False
        return True

    async def _place(self, item: _Pending) -> None:
        if not self.plan.merge:
            return  # файл уже под своим именем во временной папке
        try:
            await self.remote.put_in_place(
                self.sid, item.work, item.final, replace=item.replace, companion=self.t.companion
            )
        except (SftpTargetExistsError, SftpPermissionError, SftpIsADirectoryError) as e:
            await self._discard(item.work)
            self._drop(item.file, e.detail)
            return
        self._temps.discard(item.work)

    async def _discard(self, work: str) -> None:
        with contextlib.suppress(AppError):
            await self.remote.delete_file(self.sid, work, self.t.companion)
        self._temps.discard(work)


class DownloadFolder(FolderJob):
    """Папка сервера на этот компьютер."""

    def __init__(
        self, manager: TransferManager, transfer: Transfer, source: str, target: Path,
        *, merge: bool,
    ) -> None:  # fmt: skip
        super().__init__(manager, transfer)
        self.source = source
        self.target = target
        self.merge = merge
        # Новая папка собирается во временной рядом с целевой
        self.root = target if merge else local.part_dir(target, transfer.id)
        self._parts: set[Path] = set()  # временные файлы (объединение папок)

    async def run(self) -> None:
        m, t = self.m, self.t
        try:
            t.companion = await m._companion(t)
            tree = await self.remote.tree(
                self.sid,
                self.source,
                t.companion,
                limit=settings.SFTP_FOLDER_LIMIT,
                stopped=lambda: t.cancel_requested,
            )
            self._account(tree)
            m._check_cancel(t)
            await m._set_status(t, "running")
            if not self.merge:
                local.make_dir(self.root)
            await self._dir(tree.root, self.source, self.root, listed=self.merge)
            m._check_cancel(t)
            await m._close_companion(t)
            if not self.merge:
                await m._set_status(t, "verifying")
                await self._into_place()
        except BaseException:
            await m._close_companion(t)
            if not self.merge:
                await m.remove_local(self.root, folder=True)
            for part in list(self._parts):
                await m.remove_local(part, late=True)
            raise
        self._finish()

    async def _into_place(self) -> None:
        """Готовая временная папка на место целевой. На Windows переименование папки
        отказывает, пока файл в ней открыт другой программой (антивирус проверяет новые
        файлы): несколько попыток с паузой."""
        for attempt in range(RENAME_TRIES):
            try:
                local.replace_dir(self.root, self.target)
                return
            except SftpLocalPermissionError:
                if attempt == RENAME_TRIES - 1:
                    raise
                await asyncio.sleep(RENAME_PAUSE)

    async def _dir(self, item: DirItem, remote_dir: str, local_dir: Path, *, listed: bool) -> None:
        """Файлы и папки item из папки сервера remote_dir в local_dir. listed: local_dir
        была до передачи, и в ней может лежать что-то с теми же именами."""
        t = self.t
        taken = await asyncio.to_thread(local.taken_names, local_dir) if listed else {}
        seen: set[str] = set()
        pending: list[_Pending] = []
        for file in item.files:
            self.m._check_cancel(t)
            key = local.name_key(file.name)
            problem = local.name_problem(file.name)
            if problem is not None:
                self._skip(file, f"The name cannot be used on this computer: it has {problem}")
            elif key in seen:
                self._skip(file, REASON_CASE)
            elif taken.get(key) is True:
                self._skip(file, REASON_DIR_HERE)
            else:
                seen.add(key)
                final = local_dir / file.name
                work = local.part_file(final, t.id) if self.merge else final
                got = await self._fetch(file, paths.join(remote_dir, file.name), work)
                if got is not None:
                    pending.append(_Pending(file, work, final, *got, replace=key in taken))
            if self._full(pending):
                await self._settle(pending)
                pending = []
        await self._settle(pending)
        for sub in item.dirs:
            self.m._check_cancel(t)
            key = local.name_key(sub.name)
            problem = local.name_problem(sub.name)
            path = local_dir / sub.name
            if problem is not None:
                self._skip_dir(sub, f"The name cannot be used on this computer: it has {problem}")
            elif key in seen:
                self._skip_dir(sub, REASON_CASE)
            elif key in taken:
                seen.add(key)
                if taken[key]:
                    await self._dir(sub, paths.join(remote_dir, sub.name), path, listed=True)
                else:
                    self._skip_dir(sub, REASON_FILE_HERE)
            else:
                seen.add(key)
                try:
                    local.make_dir(path)
                except (SftpLocalError, SftpLocalPermissionError) as e:
                    self._skip_dir(sub, e.detail)
                    continue
                await self._dir(sub, paths.join(remote_dir, sub.name), path, listed=False)

    def _source(self, file: FileItem) -> str:
        return paths.join(self.source, file.rel)

    async def _fetch(self, file: FileItem, source: str, work: Path) -> tuple[str, int] | None:
        """Файл с сервера. (SHA-256, размер) полученного файла или None, если пропущен."""
        t = self.t
        t.current = file.rel
        if self.merge:
            self._parts.add(work)
        try:
            await self.m._with_progress(
                t,
                self.remote.download(self.sid, source, str(work), t.companion),
                lambda: self._downloaded(work),
                done_after=self.base + file.size,
            )
        except (SftpLocalError, SftpPermissionError, SftpNotFoundError) as e:
            await self._discard(work)
            self._skip(file, e.detail)
            return None
        self._sent(file)
        try:
            return await _hash_in_thread(work)
        except OSError as e:
            await self._discard(work)
            self._drop(file, f"Cannot read the downloaded file: {e.strerror or e}")
            return None

    async def _downloaded(self, work: Path) -> int | None:
        try:
            return self.base + work.stat().st_size
        except OSError:
            return None

    def _remote_side(self, item: _Pending) -> str:
        return self._source(item.file)

    async def _again(self, item: _Pending) -> bool:
        try:
            await self.remote.download(
                self.sid, self._source(item.file), str(item.work), self.t.companion
            )
        except (SftpLocalError, SftpPermissionError, SftpNotFoundError):
            return False
        try:
            item.digest, item.size = await _hash_in_thread(item.work)
        except OSError:
            return False
        return True

    async def _place(self, item: _Pending) -> None:
        if not self.merge:
            return  # файл уже под своим именем во временной папке
        try:
            local.replace(item.work, item.final)
        except (SftpLocalError, SftpLocalPermissionError, SftpLocalExistsError) as e:
            await self._discard(item.work)
            self._drop(item.file, e.detail)
            return
        self._parts.discard(item.work)

    async def _discard(self, work: Path) -> None:
        await self.m.remove_local(work)
        self._parts.discard(work)


def _totals(item: DirItem) -> tuple[int, int]:
    """Сколько файлов и байт в папке со всеми вложенными."""
    files, size = len(item.files), sum(file.size for file in item.files)
    for sub in item.dirs:
        sub_files, sub_size = _totals(sub)
        files, size = files + sub_files, size + sub_size
    return files, size


def _digest(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as file:
        while chunk := file.read(HASH_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _hash_in_thread(path: Path) -> asyncio.Future[tuple[str, int]]:
    """SHA-256 и размер файла этого компьютера, в потоке. Ошибку чтения (OSError) получит
    тот, кто дождётся результата; если никто не дождался, она не шумит в логе."""
    future = asyncio.ensure_future(asyncio.to_thread(_digest, path))
    future.add_done_callback(lambda done: done.cancelled() or done.exception())
    return future
