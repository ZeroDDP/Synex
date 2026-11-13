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
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import secrets
import time
from collections.abc import Awaitable, Callable
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
from app.plugins.sftp.remote import Entry, RemoteFS, UploadPlan
from app.plugins.ssh.errors import SSHNotConnectedError, SSHSessionNotFoundError
from app.plugins.ssh.session import CompanionSession

logger = structlog.get_logger()

TRANSFER_EVENT = "sftp.transfer"

Direction = Literal["upload", "download"]
Source = Literal["path", "http"]
Status = Literal["queued", "running", "verifying", "completed", "failed", "cancelled"]
ACTIVE: frozenset[str] = frozenset({"queued", "running", "verifying"})
HASH_CHUNK = 1024 * 1024

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
    bytes_total: int | None
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
        """Файл этого компьютера на сервер. Ошибки «куда» и «откуда» (нет файла, место
        занято) выбрасываются сразу, сама передача идёт в фоне. name: имя файла на
        сервере, если remote_path это папка (по умолчанию имя локального файла)."""
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
        """Файл сервера на этот компьютер. Папка в local_path значит «в неё под тем же
        именем»."""
        entry = await self.remote.remote_file(session_id, remote_path)
        target = local.target_file(local_path, entry.name, overwrite=overwrite)
        transfer = self._new(
            _new_id(), session_id, "download", source, entry.path,
            str(target) if source == "path" else None, entry.size,
        )  # fmt: skip
        await self._publish_now(transfer)
        self._start(transfer, self._run_download(transfer, entry, target))
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
            # Воркер не умеет прерывать передачу: закрываем её служебную сессию
            await self.remote.manager.close_companion(transfer.companion)
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
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, *self._background, return_exceptions=True)

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
    ) -> Transfer:
        transfer = Transfer(
            id=transfer_id,
            session_id=session_id,
            direction=direction,
            source=source,
            name=remote_path.rsplit("/", 1)[-1],
            remote_path=remote_path,
            local_path=local_path,
            bytes_total=size,
        )
        self._transfers[transfer.id] = transfer
        self._prune()
        return transfer

    def _start(self, transfer: Transfer, work: Awaitable[None]) -> None:
        async def queued() -> None:
            queue = self._queues.setdefault(transfer.session_id, LoopLock())
            try:
                async with queue:
                    await self._run(transfer, work)
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

    async def _run(self, transfer: Transfer, work: Awaitable[None]) -> None:
        transfer.started_at = _now()
        await self._set_status(transfer, "running")
        try:
            await work
        except asyncio.CancelledError:
            raise
        except _CancelledError:
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
        try:
            transfer.companion = await self._companion(transfer)
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
            with contextlib.suppress(OSError):
                part.unlink()
            raise

    async def _with_progress(
        self,
        transfer: Transfer,
        work: Awaitable[None],
        measure: Callable[[], Awaitable[int | None]],
    ) -> None:
        """Пока идёт передача, отмечать прогресс."""
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
        if transfer.bytes_total is not None:
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
            raise _CancelledError

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


class _CancelledError(Exception):
    pass


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
