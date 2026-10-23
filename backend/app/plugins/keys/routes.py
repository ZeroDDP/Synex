from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Response

from app.api.deps import Keys
from app.keystore import InvalidKeyError
from app.keystore.ssh_keys import MAX_KEY_SIZE
from app.plugins.keys.schemas import KeyGenerateRequest, KeyImportRequest, KeyInfo, KeyUpdateRequest

router = APIRouter()

ERRORS = {
    404: {"description": "Ключ не найден"},
    409: {"description": "Имя занято или этот ключ уже сохранён"},
    500: {"description": "Файл хранилища повреждён или зашифрован другим мастер-ключом"},
    503: {"description": "Системное хранилище мастер-ключа недоступно"},
}


@router.get("", response_model=list[KeyInfo], summary="Список сохранённых ключей")
async def list_keys(keys: Keys) -> list[KeyInfo]:
    return [KeyInfo.from_record(record) for record in await keys.list_keys()]


@router.post(
    "",
    response_model=KeyInfo,
    status_code=201,
    summary="Импортировать приватный ключ",
    responses={**ERRORS, 422: {"description": "Ключ не читается или нужна парольная фраза"}},
)
async def import_key(request: KeyImportRequest, keys: Keys) -> KeyInfo:
    if request.private_key is not None:
        data = request.private_key.get_secret_value().encode()
    else:
        assert request.private_key_path is not None
        data = _read_key_file(Path(request.private_key_path))
    passphrase = request.passphrase.get_secret_value().encode() if request.passphrase else None
    record = await keys.import_key(request.name, data, passphrase, request.comment)
    return KeyInfo.from_record(record)


@router.post(
    "/generate",
    response_model=KeyInfo,
    status_code=201,
    summary="Сгенерировать новый ключ",
    description="Публичную часть (`public_key`) нужно добавить в ~/.ssh/authorized_keys сервера.",
    responses=ERRORS,
)
async def generate_key(request: KeyGenerateRequest, keys: Keys) -> KeyInfo:
    record = await keys.generate_key(request.name, request.type, request.bits, request.comment)
    return KeyInfo.from_record(record)


@router.get("/{key_id}", response_model=KeyInfo, summary="Ключ по ID", responses=ERRORS)
async def get_key(key_id: str, keys: Keys) -> KeyInfo:
    return KeyInfo.from_record(await keys.get_key(key_id))


@router.patch(
    "/{key_id}",
    response_model=KeyInfo,
    summary="Переименовать ключ или сменить комментарий",
    responses=ERRORS,
)
async def update_key(key_id: str, request: KeyUpdateRequest, keys: Keys) -> KeyInfo:
    record = await keys.update_key(key_id, name=request.name, comment=request.comment)
    return KeyInfo.from_record(record)


@router.delete("/{key_id}", status_code=204, summary="Удалить ключ", responses=ERRORS)
async def delete_key(key_id: str, keys: Keys) -> Response:
    await keys.delete_key(key_id)
    return Response(status_code=204)


def _read_key_file(path: Path) -> bytes:
    try:
        if path.stat().st_size > MAX_KEY_SIZE:
            raise InvalidKeyError(f"{path} is too large to be a private key")
        return path.read_bytes()
    except OSError as e:
        raise InvalidKeyError(f"Cannot read {path}: {e.strerror or e}") from e
