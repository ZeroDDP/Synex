"""Передача файлов между этим компьютером и сервером, с прогрессом для UI.

* Передача по пути на этом компьютере идёт в фоне: запрос сразу отвечает 202, а
  передачи одной сессии выполняются по очереди. Загрузка и скачивание телом HTTP-запроса
  или ответа идут через временный файл backend и тем же путём.
* Файл передаёт SFTP воркера в служебной сессии к тому же серверу (у неё своё
  IPC-подключение): основная сессия свободна, терминал и метрики работают. Если
  служебную сессию открыть нельзя, передача идёт в основной.
* Прогресс: при скачивании растёт временный файл на этом компьютере, при загрузке
  backend раз в SFTP_PROGRESS_INTERVAL спрашивает размер временного файла на сервере
  через основную сессию (если она занята командой пользователя, отметка пропускается).
  Событие sftp.transfer уходит в шину (хаб пересылает его в /ws) при каждой смене
  статуса и не чаще раза в SFTP_PROGRESS_INTERVAL, пока идут данные.
* Файл никогда не появляется на месте наполовину: данные пишутся во временный файл,
  сверяются размер и SHA-256, и только потом он заменяет целевой. При ошибке и отмене
  временный файл удаляется.
* Отмена: идущая передача обрывается закрытием служебной сессии. Передачу в основной
  сессии воркер прервать не может: она отменяется, как только воркер закончит.
* Папка целиком это тоже одна передача (kind = "dir"): backend обходит её и передаёт по
  файлу, см. folders.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import secrets
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import structlog

from app.core.config import settings
from app.core.errors import AppError
from app.core.event_bus import event_bus
from app.core.locks import LoopLock
from app.plugins.sftp import local
from app.plugins.sftp.errors import (
    SftpCommandError,
    SftpTransferCancelledError,
    SftpTransferCorruptedError,
    SftpTransferNotFoundError,
)
from app.plugins.sftp.folders import DownloadFolder, UploadFolder
from app.plugins.sftp.remote import Entry, RemoteFS, UploadPlan
from app.plugins.sftp.tree import TransferStoppedError
from app.plugins.ssh.errors import SSHNotConnectedError, SSHSessionNotFoundError
from app.plugins.ssh.session import CompanionSession

logger = structlog.get_logger()

TRANSFER_EVENT = "sftp.transfer"

Direction = Literal["upload", "download"]
Source = Literal["path", "http"]
Kind = Literal["file", "dir"]
Status = Literal[
    "queued", "scanning", "running", "verifying", "completed", "failed", "cancelled"
]  # fmt: skip
ACTIVE: frozenset[str] = frozenset({"queued", "scanning", "running", "verifying"})
HASH_CHUNK = 1024 * 1024
# Временные файлы, которые не удалились сразу (на Windows файл отменённой передачи ещё
# держит воркер): сколько раз и с какой паузой повторять, сек
CLEANUP_TRIES = 60
CLEANUP_PAUSE = 0.5
# Воркер не прерывает действие, подключение которого закрыли (так отменяется передача): он
# доделывает его в фоне. Если передачу оборвали в самом начале файла, воркер создаёт этот
# файл уже после того, как backend убрал временные файлы. Поэтому путь, которого при
# уборке ещё не было, проверяется дальше: столько секунд на этом компьютере и с такими
# паузами (сек) на сервере.
LATE_SECONDS = 15.0
SWEEP_PAUSES: tuple[float, ...] = (0.5, 1, 1.5, 2, 3, 3, 4)
SWEEP_WAIT = 5.0  # сколько проверка на сервере ждёт основную сессию, если служебной нет

Publish = Callable[[dict[str, Any]], Awaitable[None]]


def _now() -> datetime:
    return datetime.now(UTC)


async def _publish_to_bus(info: dict[str, Any]) -> None:
    await event_bus.publish(TRANSFER_EVENT, info)


@dataclass(eq=False)
class Transfer:
    id: str
    session_id: str
    direction: Direction
    source: Source
    name: str
    remote_path: str
    local_path: str | None  # None у передачи телом HTTP
    bytes_total: int | None  # None у папки, пока идёт обход
    kind: Kind = "file"
    status: Status = "queued"
    bytes_done: int = 0
    error: str | None = None
    detail: str | None = None
    checksum: str | None = None
    verified: bool | None = None  # SHA-256 сверен (None: нечем сверить на сервере)
    created_at: datetime = field(default_factory=_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    speed: float | None = None  # байт в секунду, сглаженная
    # Папка: сколько файлов передать и сколько уже на месте, какой файл идёт сейчас (путь
    # от корня папки), что пропущено (первые несколько с причиной и общий счёт)
    files_total: int | None = None
    files_done: int = 0
    current: str | None = None
    skipped: list[dict[str, str]] = field(default_factory=list)
    skipped_count: int = 0
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    companion: CompanionSession | None = field(default=None, repr=False)
    cancel_requested: bool = False
    _mark: tuple[float, int] | None = field(default=None, repr=False)
    _published_at: float = field(default=0.0, repr=False)

    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    @property
    def percent(self) -> float | None:
        if self.bytes_total is None:
            return None
        if self.bytes_total == 0:
            return 100.0 if self.status == "completed" else 0.0
        return round(min(self.bytes_done, self.bytes_total) * 100 / self.bytes_total, 1)

    def advance(self, done: int) -> None:
        """Новая отметка прогресса: сколько байт уже передано."""
        done = max(done, self.bytes_done)
        now = time.monotonic()
        if self._mark is not None and done > self._mark[1]:
            elapsed = now - self._mark[0]
            if elapsed > 0:
                current = (done - self._mark[1]) / elapsed
                self.speed = current if self.speed is None else 0.5 * self.speed + 0.5 * current
        if self._mark is None or done > self._mark[1]:
            self._mark = (now, done)
        self.bytes_done = done

    def info(self) -> dict[str, Any]:
        eta = None
        if self.speed and self.bytes_total is not None and self.status == "running":
            eta = max(0, round((self.bytes_total - self.bytes_done) / self.speed))
        return {
            "id": self.id,
            "session_id": self.session_id,
            "kind": self.kind,
            "direction": self.direction,
            "source": self.source,
            "name": self.name,
            "remote_path": self.remote_path,
            "local_path": self.local_path,
            "status": self.status,
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
            "percent": self.percent,
            "speed": round(self.speed) if self.speed is not None else None,
            "eta_seconds": eta,
            "files_total": self.files_total,
            "files_done": self.files_done,
            "current": self.current,
            "skipped": list(self.skipped),
            "skipped_count": self.skipped_count,
            "error": self.error,
            "detail": self.detail,
            "checksum": self.checksum,
            "verified": self.verified,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class TransferManager:
    def __init__(self, remote: RemoteFS, publish: Publish | None = None) -> None:
        self.remote = remote
        self._publish = publish or _publish_to_bus
        self._transfers: dict[str, Transfer] = {}
        self._queues: dict[str, LoopLock] = {}  # очередь передач сессии
        self._background: set[asyncio.Task[Any]] = set()

    # ----------------------------------------------------------------- start

    async def upload_path(
        self,
        session_id: str,
        local_path: str,
        remote_path: str,
        *,
        overwrite: bool,
        source: Source = "path",
        name: str | None = None,
    ) -> Transfer:
        """Файл или папка этого компьютера на сервер. Ошибки «куда» и «откуда» (нет
        файла, место занято) выбрасываются сразу, сама передача идёт в фоне. name: имя
        файла на сервере, если remote_path это папка (по умолчанию имя локального файла)."""
        if source == "path" and local.is_dir(local_path):
            return await self._upload_dir(session_id, local_path, remote_path, overwrite)
        file, size = local.source_file(local_path)
        transfer_id = _new_id()
        plan = await self.remote.plan_upload(
            session_id, remote_path, name or file.name, transfer_id, overwrite=overwrite
        )
        transfer = self._new(
            transfer_id, session_id, "upload", source, plan.target,
            str(file) if source == "path" else None, size,
        )  # fmt: skip
        await self._publish_now(transfer)
        self._start(transfer, self._run_upload(transfer, plan, file))
        return transfer

    async def download_path(
        self,
        session_id: str,
        remote_path: str,
        local_path: str,
        *,
        overwrite: bool,
        source: Source = "path",
    ) -> Transfer:
        """Файл или папка сервера на этот компьютер. Папка в local_path значит «в неё
        под тем же именем»."""
        entry = await self.remote.stat(session_id, remote_path)
        if entry.is_dir and source == "path":
            return await self._download_dir(session_id, entry, local_path, overwrite)
        entry = self.remote.as_file(entry)
        target = local.target_file(local_path, entry.name, overwrite=overwrite)
        transfer = self._new(
            _new_id(), session_id, "download", source, entry.path,
            str(target) if source == "path" else None, entry.size,
        )  # fmt: skip
        await self._publish_now(transfer)
        self._start(transfer, self._run_download(transfer, entry, target))
        return transfer

    async def _upload_dir(
        self, session_id: str, local_path: str, remote_path: str, overwrite: bool
    ) -> Transfer:
        """Папка целиком на сервер; overwrite: объединить с папкой, которая там уже есть."""
        folder = local.source_dir(local_path)
        transfer_id = _new_id()
        plan = await self.remote.plan_upload_dir(
            session_id, remote_path, folder.name, transfer_id, overwrite=overwrite
        )
        transfer = self._new(
            transfer_id, session_id, "upload", "path", plan.target, str(folder), None, "dir"
        )
        await self._publish_now(transfer)
        job = UploadFolder(self, transfer, folder, plan)
        self._start(transfer, job.run(), first="scanning")
        return transfer

    async def _download_dir(
        self, session_id: str, entry: Entry, local_path: str, overwrite: bool
    ) -> Transfer:
        """Папка целиком с сервера; overwrite: объединить с папкой, которая здесь уже есть."""
        target, merge = local.target_dir(local_path, entry.name, overwrite=overwrite)
        transfer = self._new(
            _new_id(), session_id, "download", "path", entry.path, str(target), None, "dir"
        )
        await self._publish_now(transfer)
        job = DownloadFolder(self, transfer, entry.path, target, merge=merge)
        self._start(transfer, job.run(), first="scanning")
        return transfer

    async def wait(self, transfer: Transfer) -> Transfer:
        """Дождаться конца передачи (HTTP-загрузка и скачивание). Ошибку передачи
        выбрасывает как ошибку API."""
        if transfer.task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(transfer.task)
        if transfer.status == "completed":
            return transfer
        if transfer.status == "cancelled":
            raise SftpTransferCancelledError(transfer.detail or "The transfer was cancelled")
        raise _ERRORS.get(transfer.error or "", SftpCommandError)(
            transfer.detail or "The transfer failed"
        )

    # ------------------------------------------------------------- queries

    def get(self, transfer_id: str) -> Transfer:
        try:
            return self._transfers[transfer_id]
        except KeyError:
            raise SftpTransferNotFoundError(f"Transfer '{transfer_id}' not found") from None

    def select(
        self, *, session_id: str | None = None, active: bool | None = None
    ) -> list[Transfer]:
        return [
            t
            for t in self._transfers.values()
            if (session_id is None or t.session_id == session_id)
            and (active is None or t.active == active)
        ]

    async def cancel(self, transfer_id: str) -> Transfer:
        """Отменить идущую передачу или убрать завершённую из списка."""
        transfer = self.get(transfer_id)
        if not transfer.active:
            del self._transfers[transfer.id]
            return transfer
        transfer.cancel_requested = True
        if transfer.status == "queued" and transfer.task is not None:
            transfer.task.cancel()
        elif transfer.companion is not None:
            companion = transfer.companion
            if companion.channel.native_cancel:
                try:
                    await companion.channel.client.request(
                        "cancel", {"target_id": companion.id}, timeout=5
                    )
                except AppError:
                    await self.remote.manager.close_companion(companion)
            else:
                await self.remote.manager.close_companion(companion)
        if transfer.task is not None:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(asyncio.shield(transfer.task), timeout=5)
        return transfer

    async def shutdown(self) -> None:
        tasks = [t.task for t in self._transfers.values() if t.task is not None and t.active]
        for transfer in self._transfers.values():
            if transfer.active:
                transfer.cancel_requested = True
                if transfer.companion is not None:
                    await self.remote.manager.close_companion(transfer.companion)
        for task in (*tasks, *self._background):
            task.cancel()
        await asyncio.gather(*tasks, *self._background, return_exceptions=True)

    async def remove_local(self, path: Path, *, folder: bool = False, late: bool = False) -> None:
        """Убрать временный файл или временную папку этого компьютера. Если сразу не
        вышло (Windows не даёт удалить файл, который ещё держит воркер), повторять в
        фоне. late: воркер мог не успеть создать файл до отмены и создаст его позже;
        файл, которого ещё нет, проверяется LATE_SECONDS."""
        remove = local.remove_tree if folder else local.remove_file

        def attempt() -> tuple[bool, bool]:
            """(путь был, теперь его нет)"""
            return os.path.lexists(path), remove(path)

        existed, gone = await asyncio.to_thread(attempt)
        if gone and (existed or not late):
            return

        async def retry() -> None:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + LATE_SECONDS
            gone = False
            for _ in range(CLEANUP_TRIES):
                await asyncio.sleep(CLEANUP_PAUSE)
                existed, gone = await asyncio.to_thread(attempt)
                # Файл появился и убран: второй раз воркер его не создаст
                if gone and (existed or not late or loop.time() >= deadline):
                    return
            if not gone:
                logger.warning("Temporary files of a transfer were not removed", path=str(path))

        self._later(retry())

    async def remove_remote(
        self, session_id: str, *, files: Iterable[str] = (), folder: str | None = None
    ) -> None:
        """Убрать с сервера временные файлы и временную папку передачи после сбоя или
        отмены. Сразу, в основной сессии; а то, что убрать не вышло, и файлы, которых ещё
        нет (воркер мог не успеть создать файл до отмены и создаст его позже), проверяются
        в фоне ещё несколько раз."""
        left = [
            path
            for path in files
            if await self.remote.discard(session_id, path) != "removed"
        ]  # fmt: skip
        if folder is not None:
            # Папки нет или она убрана целиком: создать в ней файл воркер уже не сможет
            if await self.remote.discard(session_id, folder, folder=True) != "failed":
                folder = None
        if left or folder is not None:
            self._later(self._sweep_remote(session_id, left, folder))

    async def _sweep_remote(self, session_id: str, files: list[str], folder: str | None) -> None:
        """Проверять временные пути на сервере, пока воркер может их создать. В служебной
        сессии, чтобы не ждать основную (в ней может идти команда пользователя)."""
        manager = self.remote.manager
        companion: CompanionSession | None = None
        failed: set[str] = set()
        try:
            with contextlib.suppress(AppError):
                companion = await manager.open_companion(session_id, "sftp")
            for pause in SWEEP_PAUSES:
                await asyncio.sleep(pause)
                manager.get(session_id)  # сессию закрыли: убирать больше нечем
                failed.clear()
                for path in list(files):
                    state = await self.remote.discard(
                        session_id, path, companion=companion, wait=SWEEP_WAIT
                    )
                    if state == "removed":
                        files.remove(path)  # появился и убран: второй раз его не создадут
                    elif state == "failed":
                        failed.add(path)
                if folder is not None:
                    state = await self.remote.discard(
                        session_id, folder, folder=True, companion=companion, wait=SWEEP_WAIT
                    )
                    if state == "failed":
                        failed.add(folder)
                    else:
                        folder = None
                if not files and folder is None:
                    return
            if failed:
                logger.warning(
                    "Temporary files of a transfer stay on the server",
                    session_id=session_id,
                    paths=sorted(failed)[:5],
                    count=len(failed),
                )
        except AppError as e:
            logger.info(
                "Temporary files of a transfer were not checked again",
                session_id=session_id,
                error=e.detail,
            )
        finally:
            if companion is not None:
                await manager.close_companion(companion)

    def _later(self, work: Coroutine[Any, Any, None]) -> None:
        """Работа в фоне, которую отменяет shutdown."""
        task = asyncio.ensure_future(work)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ------------------------------------------------------------- running

    def _new(
        self,
        transfer_id: str,
        session_id: str,
        direction: Direction,
        source: Source,
        remote_path: str,
        local_path: str | None,
        size: int | None,
        kind: Kind = "file",
    ) -> Transfer:
        transfer = Transfer(
            id=transfer_id,
            session_id=session_id,
            direction=direction,
            source=source,
            name=remote_path.rsplit("/", 1)[-1] or "/",
            remote_path=remote_path,
            local_path=local_path,
            bytes_total=size,
            kind=kind,
        )
        self._transfers[transfer.id] = transfer
        self._prune()
        return transfer

    def _start(
        self, transfer: Transfer, work: Awaitable[None], *, first: Status = "running"
    ) -> None:
        """first: с какого статуса начинается работа (у папки с обхода)."""

        async def queued() -> None:
            queue = self._queues.setdefault(transfer.session_id, LoopLock())
            try:
                async with queue:
                    await self._run(transfer, work, first)
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    work.close()  # type: ignore[attr-defined]
                if transfer.active:
                    await self._finish(
                        transfer, "cancelled", "sftp_transfer_cancelled",
                        "The transfer was cancelled",
                    )  # fmt: skip
                raise

        transfer.task = asyncio.ensure_future(queued())

    async def _run(self, transfer: Transfer, work: Awaitable[None], first: Status) -> None:
        transfer.started_at = _now()
        await self._set_status(transfer, first)
        try:
            await work
        except asyncio.CancelledError:
            raise
        except TransferStoppedError:
            await self._finish(
                transfer, "cancelled", "sftp_transfer_cancelled", "The transfer was cancelled"
            )
        except (SSHNotConnectedError, SSHSessionNotFoundError) as e:
            if transfer.cancel_requested:
                await self._finish(
                    transfer, "cancelled", "sftp_transfer_cancelled",
                    "The transfer was cancelled",
                )  # fmt: skip
            else:
                await self._finish(transfer, "failed", e.code, e.detail)
        except AppError as e:
            await self._finish(transfer, "failed", e.code, e.detail)
        except Exception as e:
            logger.exception("SFTP transfer failed", transfer_id=transfer.id, error=repr(e))
            await self._finish(transfer, "failed", "sftp_failed", f"Unexpected error: {e!r}")
        else:
            await self._finish(transfer, "completed")

    async def _run_upload(self, transfer: Transfer, plan: UploadPlan, file: Path) -> None:
        remote = self.remote
        hashing = asyncio.ensure_future(asyncio.to_thread(_sha256, file))
        await remote.begin_upload(plan)
        try:
            transfer.companion = await self._companion(transfer)
            await self._with_progress(
                transfer,
                remote.upload(plan, str(file), transfer.companion),
                lambda: remote.temp_size(plan),
            )
            await self._close_companion(transfer)
            self._check_cancel(transfer)
            await self._set_status(transfer, "verifying")
            size = await remote.file_size(plan.session_id, plan.temp_file)
            local_hash = await hashing
            await self._verify(transfer, size, local_hash, plan.session_id, plan.temp_file)
            self._check_cancel(transfer)
            await remote.commit_upload(plan)
        except BaseException:
            hashing.cancel()
            await self._close_companion(transfer)
            await remote.abort_upload(plan)
            raise

    async def _run_download(self, transfer: Transfer, entry: Entry, target: Path) -> None:
        remote = self.remote
        part = local.part_file(target, transfer.id)
        asked = False  # воркер получил запрос и может создать файл и после отмены
        try:
            transfer.companion = await self._companion(transfer)
            asked = True
            await self._with_progress(
                transfer,
                remote.download(transfer.session_id, entry.path, str(part), transfer.companion),
                lambda: _local_size(part),
            )
            await self._close_companion(transfer)
            self._check_cancel(transfer)
            await self._set_status(transfer, "verifying")
            local_hash = await asyncio.to_thread(_sha256, part)
            await self._verify(
                transfer, _sync_size(part), local_hash, transfer.session_id, entry.path
            )
            self._check_cancel(transfer)
            local.replace(part, target)
        except BaseException:
            await self._close_companion(transfer)
            await self.remove_local(part, late=asked)
            raise

    async def _with_progress(
        self,
        transfer: Transfer,
        work: Awaitable[None],
        measure: Callable[[], Awaitable[int | None]],
        *,
        done_after: int | None = None,
    ) -> None:
        """Пока идёт передача файла, отмечать прогресс. done_after: сколько байт передачи
        готово после этого файла (у папки); без него файл и есть вся передача."""
        task = asyncio.ensure_future(work)
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=settings.SFTP_PROGRESS_INTERVAL)
                if done:
                    break
                size = await measure()
                if size is not None:
                    transfer.advance(size)
                    await self._progress(transfer)
            task.result()
        except BaseException:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            raise
        if done_after is not None:
            transfer.advance(done_after)
            await self._progress(transfer)
        elif transfer.bytes_total is not None:
            transfer.advance(transfer.bytes_total)
            await self._progress(transfer, force=True)

    async def _verify(
        self,
        transfer: Transfer,
        size: int | None,
        local_hash: str,
        session_id: str,
        remote_path: str,
    ) -> None:
        if transfer.bytes_total is not None and size != transfer.bytes_total:
            raise SftpTransferCorruptedError(
                f"Size mismatch after the transfer: {size} bytes instead of "
                f"{transfer.bytes_total}. The file was not saved"
            )
        transfer.checksum = local_hash
        if not settings.SFTP_VERIFY:
            return
        remote_hash = await self.remote.checksum(session_id, remote_path)
        if remote_hash is None:
            return  # на сервере нечем посчитать SHA-256: сверен только размер
        if remote_hash != local_hash:
            raise SftpTransferCorruptedError(
                "SHA-256 of the file differs on this computer and on the server. "
                "The file was not saved"
            )
        transfer.verified = True

    async def _companion(self, transfer: Transfer) -> CompanionSession | None:
        try:
            return await self.remote.manager.open_companion(transfer.session_id, "sftp")
        except AppError as e:
            logger.warning(
                "Companion session for a transfer failed, using the main session",
                transfer_id=transfer.id,
                error=e.detail,
            )
            return None

    async def _close_companion(self, transfer: Transfer) -> None:
        companion, transfer.companion = transfer.companion, None
        if companion is not None:
            await self.remote.manager.close_companion(companion)

    @staticmethod
    def _check_cancel(transfer: Transfer) -> None:
        if transfer.cancel_requested:
            raise TransferStoppedError

    # ------------------------------------------------------------- events

    async def _set_status(self, transfer: Transfer, status: Status) -> None:
        transfer.status = status
        await self._publish_now(transfer)

    async def _finish(
        self,
        transfer: Transfer,
        status: Status,
        error: str | None = None,
        detail: str | None = None,
    ) -> None:
        transfer.status = status
        transfer.error = error
        transfer.detail = detail
        transfer.finished_at = _now()
        if status != "completed":
            transfer.speed = None
        log = logger.info if status == "completed" else logger.warning
        log(
            "SFTP transfer finished",
            transfer_id=transfer.id,
            direction=transfer.direction,
            status=status,
            bytes=transfer.bytes_done,
            error=error,
        )
        await self._publish_now(transfer)

    async def _progress(self, transfer: Transfer, *, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - transfer._published_at >= settings.SFTP_PROGRESS_INTERVAL:
            await self._publish_now(transfer)

    async def _publish_now(self, transfer: Transfer) -> None:
        transfer._published_at = time.monotonic()
        try:
            await self._publish(transfer.info())
        except Exception as e:
            logger.warning("Cannot publish SFTP transfer event", error=repr(e))

    def _prune(self) -> None:
        finished = [t for t in self._transfers.values() if not t.active]
        for transfer in finished[: max(0, len(finished) - settings.SFTP_TRANSFER_HISTORY)]:
            del self._transfers[transfer.id]


# Код ошибки передачи → класс ошибки API (чтобы HTTP-загрузка ответила тем же кодом)
_ERRORS: dict[str, type[AppError]] = {}


def _register_errors() -> None:
    from app.plugins.sftp import errors as module

    for value in vars(module).values():
        if isinstance(value, type) and issubclass(value, AppError) and value is not AppError:
            _ERRORS[value.code] = value
    from app.plugins.ssh import errors as ssh_errors

    for value in vars(ssh_errors).values():
        if isinstance(value, type) and issubclass(value, AppError) and value is not AppError:
            _ERRORS[value.code] = value


_register_errors()


def _new_id() -> str:
    return secrets.token_hex(6)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _sync_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


async def _local_size(path: Path) -> int | None:
    return _sync_size(path)
