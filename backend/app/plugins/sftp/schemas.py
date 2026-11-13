from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.plugins.sftp.local import LocalEntry, LocalListing
from app.plugins.sftp.remote import Entry, Listing, ServerInfo

TRANSFER_ID_PATTERN = r"^[0-9a-f]{12}$"
PATH_DESCRIPTION = "Путь на сервере: абсолютный (/var/log), от домашней папки (docs) или ~/docs"


class EntryInfo(BaseModel):
    name: str
    path: str = Field(..., description="Абсолютный путь: его и передавайте в другие запросы")
    type: Literal["file", "dir", "link", "other"]
    size: int | None = Field(..., description="Байты")
    is_dir: bool = Field(..., description="Папка или ссылка на папку: открывается по клику")
    hidden: bool = Field(..., description="Имя начинается с точки (или скрыт в Windows)")
    permissions: str | None = Field(..., description='Как в ls -l: "-rw-r--r--"')
    mode: int | None = Field(..., description="Биты прав числом (0o644 = 420)")
    target_type: Literal["file", "dir"] | None = Field(
        ..., description="Куда ведёт ссылка: на папку (dir) или нет (null)"
    )
    modified_at: datetime | None = Field(
        ..., description="Время изменения; у файлов сервера пока null (SFTP воркера не отдаёт)"
    )

    @classmethod
    def from_entry(cls, entry: Entry | LocalEntry) -> EntryInfo:
        return cls(
            name=entry.name,
            path=entry.path,
            type=entry.type,
            size=entry.size,
            is_dir=entry.is_dir,
            hidden=entry.hidden,
            permissions=entry.permissions,
            mode=(entry.mode & 0o7777) if entry.mode is not None else None,
            target_type=entry.target_type,
            modified_at=entry.modified_at,
        )


class ListingInfo(BaseModel):
    path: str
    parent: str | None = Field(..., description="Папка выше (кнопка «вверх»); null в корне")
    home: str
    entries: list[EntryInfo] = Field(..., description="Сначала папки, внутри по имени")

    @classmethod
    def from_listing(cls, listing: Listing) -> ListingInfo:
        return cls(
            path=listing.path,
            parent=listing.parent,
            home=listing.home,
            entries=[EntryInfo.from_entry(e) for e in listing.entries],
        )


class LocalListingInfo(ListingInfo):
    roots: list[str] = Field(..., description='Диски Windows ("C:\\\\") или ["/"]')
    separator: str = Field(..., description="Разделитель путей этой ОС")

    @classmethod
    def from_local(cls, listing: LocalListing) -> LocalListingInfo:
        return cls(
            path=listing.path,
            parent=listing.parent,
            home=listing.home,
            roots=listing.roots,
            separator=listing.separator,
            entries=[EntryInfo.from_entry(e) for e in listing.entries],
        )


class ServerInfoResponse(BaseModel):
    engine: Literal["core"] = Field(..., description="core: SFTP Rust-воркера")
    home: str
    user: str
    checksum: bool | None = Field(
        ..., description="Можно ли сверить SHA-256 на сервере (null: ещё не проверяли)"
    )

    @classmethod
    def from_info(cls, info: ServerInfo) -> ServerInfoResponse:
        return cls(engine="core", home=info.home, user=info.user, checksum=info.checksum)


class MkdirRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=4096, description=PATH_DESCRIPTION)
    parents: bool = Field(default=False, description="Создать и недостающие папки выше")


class MoveRequest(BaseModel):
    source: str = Field(..., min_length=1, max_length=4096)
    target: str = Field(..., min_length=1, max_length=4096, description="Новый полный путь")
    overwrite: bool = Field(default=False, description="Заменить существующий файл")


class RemovedInfo(BaseModel):
    path: str
    removed: bool


class TransferRequest(BaseModel):
    direction: Literal["upload", "download"] = Field(
        ..., description="upload: с этого компьютера на сервер; download: обратно"
    )
    local_path: str = Field(
        ...,
        min_length=1,
        max_length=4096,
        description="Файл или папка на этом компьютере (абсолютный путь)",
    )
    remote_path: str = Field(
        ..., min_length=1, max_length=4096, description="Файл или папка на сервере"
    )
    overwrite: bool = Field(default=False, description="Заменить существующий файл")


class TransferInfo(BaseModel):
    id: str
    session_id: str
    direction: Literal["upload", "download"]
    source: Literal["path", "http"] = Field(
        ..., description="path: по пути на этом компьютере; http: телом запроса или ответа"
    )
    name: str
    remote_path: str
    local_path: str | None
    status: Literal["queued", "running", "verifying", "completed", "failed", "cancelled"]
    bytes_done: int
    bytes_total: int | None
    percent: float | None
    speed: int | None = Field(..., description="Байт в секунду")
    eta_seconds: int | None
    error: str | None = Field(..., description="Код ошибки, если failed или cancelled")
    detail: str | None
    checksum: str | None = Field(..., description="SHA-256 файла")
    verified: bool | None = Field(
        ..., description="SHA-256 сверен на сервере; null: на сервере нечем посчитать"
    )
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
