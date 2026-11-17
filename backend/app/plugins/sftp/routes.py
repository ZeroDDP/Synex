from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import FileResponse

from app.plugins.sftp import local
from app.plugins.sftp.errors import SftpBadPathError, SftpLocalError
from app.plugins.sftp.schemas import (
    PATH_DESCRIPTION,
    TRANSFER_ID_PATTERN,
    EntryInfo,
    ListingInfo,
    LocalListingInfo,
    MkdirRequest,
    MoveRequest,
    RemovedInfo,
    ServerInfoResponse,
    TransferInfo,
    TransferRequest,
)
from app.plugins.sftp.service import SftpService, get_sftp
from app.plugins.ssh.schemas import SESSION_ID_PATTERN

router = APIRouter()

Sftp = Annotated[SftpService, Depends(get_sftp)]
SessionId = Annotated[str, PathParam(pattern=SESSION_ID_PATTERN, description="SSH-сессия")]
TransferId = Annotated[str, PathParam(pattern=TRANSFER_ID_PATTERN)]
RemotePath = Annotated[str, Query(min_length=1, max_length=4096, description=PATH_DESCRIPTION)]
OptionalPath = Annotated[
    str | None,
    Query(max_length=4096, description=PATH_DESCRIPTION + ". Без path домашняя папка"),
]

TRANSFER_ID_HEADER = "X-Synex-Transfer-Id"
SPOOL_NAME = "file.synex-spool"  # временный файл HTTP-загрузки и скачивания
SPOOL_CHUNK = 1024 * 1024

ERRORS = {
    403: {"description": "Нет прав на сервере или путь защищён (корень, домашняя папка)"},
    404: {"description": "Нет сессии, файла или папки"},
    409: {"description": "Уже есть, папка вместо файла, папка не пуста или сессия занята"},
    422: {"description": "Неверный путь"},
    502: {"description": "Сервер или воркер не выполнил операцию (текст в detail)"},
    504: {"description": "Операция не уложилась в лимит"},
}


# ------------------------------------------------------------------- server


@router.get(
    "/sessions/{session_id}/info",
    response_model=ServerInfoResponse,
    summary="Файлы на сервере: домашняя папка, пользователь, можно ли сверять SHA-256",
    responses=ERRORS,
)
async def info(session_id: SessionId, sftp: Sftp) -> ServerInfoResponse:
    return ServerInfoResponse.from_info(await sftp.remote.info(session_id))


@router.get(
    "/sessions/{session_id}/list",
    response_model=ListingInfo,
    summary="Содержимое папки на сервере",
    responses=ERRORS,
)
async def list_directory(
    session_id: SessionId, sftp: Sftp, path: OptionalPath = None
) -> ListingInfo:
    return ListingInfo.from_listing(await sftp.remote.list(session_id, path))


@router.get(
    "/sessions/{session_id}/stat",
    response_model=EntryInfo,
    summary="Сведения о файле или папке на сервере",
    responses=ERRORS,
)
async def stat_entry(session_id: SessionId, path: RemotePath, sftp: Sftp) -> EntryInfo:
    return EntryInfo.from_entry(await sftp.remote.stat(session_id, path))


@router.post(
    "/sessions/{session_id}/mkdir",
    response_model=EntryInfo,
    status_code=201,
    summary="Создать папку",
    responses=ERRORS,
)
async def mkdir(session_id: SessionId, request: MkdirRequest, sftp: Sftp) -> EntryInfo:
    entry = await sftp.remote.mkdir(session_id, request.path, parents=request.parents)
    return EntryInfo.from_entry(entry)


@router.post(
    "/sessions/{session_id}/move",
    response_model=EntryInfo,
    summary="Переименовать или переместить",
    responses=ERRORS,
)
async def move(session_id: SessionId, request: MoveRequest, sftp: Sftp) -> EntryInfo:
    entry = await sftp.remote.move(
        session_id, request.source, request.target, overwrite=request.overwrite
    )
    return EntryInfo.from_entry(entry)


@router.post(
    "/sessions/{session_id}/copy",
    response_model=EntryInfo,
    status_code=201,
    summary="Скопировать файл или папку на сервере",
    responses=ERRORS,
)
async def copy(session_id: SessionId, request: MoveRequest, sftp: Sftp) -> EntryInfo:
    entry = await sftp.remote.copy(
        session_id, request.source, request.target, overwrite=request.overwrite
    )
    return EntryInfo.from_entry(entry)


@router.delete(
    "/sessions/{session_id}/entry",
    response_model=RemovedInfo,
    summary="Удалить файл или папку",
    responses=ERRORS,
)
async def remove(
    session_id: SessionId,
    path: RemotePath,
    sftp: Sftp,
    recursive: Annotated[bool, Query(description="Удалить папку вместе с содержимым")] = False,
) -> RemovedInfo:
    removed = await sftp.remote.remove(session_id, path, recursive=recursive)
    return RemovedInfo(path=removed, removed=True)


# ---------------------------------------------------------------- transfers


@router.post(
    "/sessions/{session_id}/transfers",
    response_model=TransferInfo,
    status_code=202,
    summary="Передать файл или папку по пути на этом компьютере (в фоне, прогресс в /ws)",
    description=(
        "Ответ сразу, со status `queued`. Прогресс: событие `sftp.transfer` в `/ws` и "
        "`GET /api/sftp/transfers/{id}`. Передачи одной сессии идут по очереди.\n\n"
        "Папка передаётся целиком одной передачей (`kind: dir`): сначала обход "
        "(`scanning`), потом файлы по одному (`files_done` из `files_total`, `current`). "
        "Новая папка появляется на месте только готовой; с `overwrite: true` папки "
        "объединяются. Ссылки и то, что нельзя прочитать или назвать, попадают в `skipped`."
    ),
    responses={
        **ERRORS,
        409: {"description": "Файл или папка уже есть (overwrite) либо на месте не тот тип"},
    },
)
async def start_transfer(
    session_id: SessionId, request: TransferRequest, sftp: Sftp
) -> TransferInfo:
    transfers = sftp.transfers
    if request.direction == "upload":
        transfer = await transfers.upload_path(
            session_id, request.local_path, request.remote_path, overwrite=request.overwrite
        )
    else:
        transfer = await transfers.download_path(
            session_id, request.remote_path, request.local_path, overwrite=request.overwrite
        )
    return TransferInfo(**transfer.info())


@router.post(
    "/sessions/{session_id}/upload",
    response_model=TransferInfo,
    status_code=201,
    summary="Загрузить файл телом запроса",
    description=(
        "Тело запроса это содержимое файла (например, файл, перетащенный в окно). Ответ "
        "после записи и сверки файла на сервере. `path`: куда положить; папка или путь с "
        "`/` в конце значит «в неё под именем `name`»."
    ),
    responses=ERRORS,
)
async def upload(
    session_id: SessionId,
    request: Request,
    path: RemotePath,
    sftp: Sftp,
    name: Annotated[
        str | None,
        Query(max_length=255, description="Имя файла, если path это папка"),
    ] = None,
    overwrite: bool = False,
) -> TransferInfo:
    file_name = _safe_name(name or path.rstrip("/").rsplit("/", 1)[-1] or "upload.bin")
    spool = await _spool_body(request)
    try:
        transfer = await sftp.transfers.upload_path(
            session_id, str(spool), path, overwrite=overwrite, source="http", name=file_name
        )
        await sftp.transfers.wait(transfer)
        return TransferInfo(**transfer.info())
    finally:
        _remove_spool(spool)


@router.get(
    "/sessions/{session_id}/download",
    summary="Скачать файл телом ответа",
    description=(
        "Файл приходит телом ответа (`Content-Disposition: attachment`), id передачи в "
        "заголовке `X-Synex-Transfer-Id`. Ответ начинается, когда файл получен с сервера "
        "и сверен; прогресс этой части идёт в `/ws`."
    ),
    response_class=FileResponse,
    responses=ERRORS,
)
async def download(
    session_id: SessionId, path: RemotePath, sftp: Sftp, background: BackgroundTasks
) -> FileResponse:
    spool_dir = Path(tempfile.mkdtemp(prefix="synex-sftp-"))
    # Своё имя файла в папке backend: имя с сервера может не подойти этой ОС ("a:b" на
    # Windows), а браузеру оно уходит в Content-Disposition
    file = spool_dir / SPOOL_NAME
    try:
        transfer = await sftp.transfers.download_path(
            session_id, path, str(file), overwrite=False, source="http"
        )
        await sftp.transfers.wait(transfer)
    except BaseException:
        _remove_spool_dir(spool_dir)
        raise
    background.add_task(_remove_spool_dir, spool_dir)
    return FileResponse(
        file,
        media_type="application/octet-stream",
        headers={
            TRANSFER_ID_HEADER: transfer.id,
            "Content-Disposition": _attachment(transfer.name),
        },
    )


@router.get(
    "/transfers",
    response_model=list[TransferInfo],
    summary="Передачи файлов: идущие и последние завершённые",
)
async def list_transfers(
    sftp: Sftp,
    session_id: Annotated[str | None, Query(pattern=SESSION_ID_PATTERN)] = None,
    active: Annotated[bool | None, Query(description="true: только идущие")] = None,
) -> list[TransferInfo]:
    selected = sftp.transfers.select(session_id=session_id, active=active)
    return [TransferInfo(**t.info()) for t in selected]


@router.get(
    "/transfers/{transfer_id}",
    response_model=TransferInfo,
    summary="Состояние передачи",
    responses={404: {"description": "Нет такой передачи"}},
)
async def get_transfer(transfer_id: TransferId, sftp: Sftp) -> TransferInfo:
    return TransferInfo(**sftp.transfers.get(transfer_id).info())


@router.delete(
    "/transfers/{transfer_id}",
    response_model=TransferInfo,
    summary="Отменить передачу или убрать завершённую из списка",
    responses={404: {"description": "Нет такой передачи"}},
)
async def cancel_transfer(transfer_id: TransferId, sftp: Sftp) -> TransferInfo:
    return TransferInfo(**(await sftp.transfers.cancel(transfer_id)).info())


# ------------------------------------------------------------ this computer


@router.get(
    "/local/list",
    response_model=LocalListingInfo,
    summary="Папка этого компьютера (вторая панель файлового менеджера)",
    responses={
        403: {"description": "Нет прав на чтение"},
        404: {"description": "Нет такой папки"},
        409: {"description": "Это не папка"},
        422: {"description": "Путь не абсолютный"},
    },
)
async def local_list(
    path: Annotated[
        str | None,
        Query(max_length=4096, description="Абсолютный путь; без path домашняя папка"),
    ] = None,
) -> LocalListingInfo:
    return LocalListingInfo.from_local(local.list_dir(path))


# ---------------------------------------------------------------- helpers


def _safe_name(name: str) -> str:
    cleaned = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if cleaned in ("", ".", "..") or any(ord(ch) < 32 for ch in cleaned):
        raise SftpBadPathError(f"Bad file name: {name!r}")
    return cleaned


async def _spool_body(request: Request) -> Path:
    """Тело запроса во временный файл backend: его отдаст воркеру SFTP. Имя файла на
    сервере передаётся отдельно, поэтому здесь своё: годится для любой ОС."""
    directory = Path(tempfile.mkdtemp(prefix="synex-sftp-"))
    spool = directory / SPOOL_NAME
    try:
        with spool.open("wb") as file:
            async for chunk in request.stream():
                file.write(chunk)
    except OSError as e:
        _remove_spool(spool)
        raise SftpLocalError(f"Cannot buffer the upload on this computer: {e}") from e
    except BaseException:
        _remove_spool(spool)
        raise
    return spool


def _remove_spool(spool: Path) -> None:
    _remove_spool_dir(spool.parent)


def _remove_spool_dir(directory: Path) -> None:
    for item in directory.glob("*"):
        with contextlib.suppress(OSError):
            item.unlink()
    with contextlib.suppress(OSError):
        directory.rmdir()


def _attachment(name: str) -> str:
    fallback = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "_" for ch in name)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name)}"
