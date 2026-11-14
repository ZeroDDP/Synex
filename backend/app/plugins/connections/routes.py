from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response
from fastapi import Path as PathParam

from app.api.deps import Keys
from app.plugins.connections.errors import (
    ConnectionNotFoundError,
    ConnectionSecretRequiredError,
    InvalidConnectionError,
)
from app.plugins.connections.schemas import (
    AUTH_FIELDS,
    CONNECTION_ID_PATTERN,
    PLAIN_FIELDS,
    ConnectionConnectRequest,
    ConnectionCreateRequest,
    ConnectionInfo,
    ConnectionUpdateRequest,
)
from app.plugins.connections.store import (
    KEEP,
    ConnectionAuth,
    ConnectionRecord,
    ConnectionStore,
    check_auth,
    get_connection_store,
)
from app.plugins.ssh.manager import ConnectParams, SSHSessionManager
from app.plugins.ssh.routes import get_ssh_manager
from app.plugins.ssh.schemas import SSHSessionInfo

router = APIRouter()

Connections = Annotated[ConnectionStore, Depends(get_connection_store)]
Manager = Annotated[SSHSessionManager, Depends(get_ssh_manager)]
ConnectionId = Annotated[str, PathParam(pattern=CONNECTION_ID_PATTERN)]

ERRORS = {
    404: {"description": "Подключение (или его ключ key_id) не найдено"},
    409: {"description": "Подключение с таким именем уже есть"},
    422: {"description": "Способ входа не согласован (invalid_connection) или неверные поля"},
    500: {"description": "connections.json или keys.json повреждён"},
    503: {"description": "Системное хранилище мастер-ключа недоступно"},
}


@router.get("", response_model=list[ConnectionInfo], summary="Сохранённые подключения")
async def list_connections(store: Connections) -> list[ConnectionInfo]:
    return [ConnectionInfo.from_record(r) for r in await store.list_connections()]


@router.post(
    "",
    response_model=ConnectionInfo,
    status_code=201,
    summary="Сохранить подключение",
    responses=ERRORS,
)
async def create_connection(
    request: ConnectionCreateRequest, store: Connections, keys: Keys
) -> ConnectionInfo:
    auth = ConnectionAuth(
        method=request.auth,
        key_id=request.key_id,
        key_path=request.key_path,
        password=_plain(request.password),
        passphrase=_plain(request.passphrase),
    )
    check_auth(auth)
    if auth.key_id is not None:
        await keys.get_key(auth.key_id)  # 404, если такого ключа нет
    record = await store.create(
        name=request.name,
        host=request.host,
        port=request.port,
        username=request.username,
        group=request.group,
        auth=auth,
    )
    return ConnectionInfo.from_record(record)


@router.get(
    "/{connection_id}", response_model=ConnectionInfo, summary="Подключение по ID", responses=ERRORS
)
async def get_connection(connection_id: ConnectionId, store: Connections) -> ConnectionInfo:
    return ConnectionInfo.from_record(await store.get(connection_id))


@router.patch(
    "/{connection_id}",
    response_model=ConnectionInfo,
    summary="Изменить подключение",
    description=(
        "Меняются только переданные поля. Новый способ входа передаётся целиком: `auth` и "
        "его поля. Без `auth` можно сменить ключ, пароль или фразу текущего способа, "
        "`password: null` забывает сохранённый пароль."
    ),
    responses=ERRORS,
)
async def update_connection(
    connection_id: ConnectionId, request: ConnectionUpdateRequest, store: Connections, keys: Keys
) -> ConnectionInfo:
    changes = {field: getattr(request, field) for field in request.model_fields_set}
    record = await store.get(connection_id)
    auth = None
    if changes.keys() & AUTH_FIELDS:
        auth = _new_auth(record, changes)
        check_auth(auth, record)
        if auth.key_id is not None and auth.key_id != record.key_id:
            await keys.get_key(auth.key_id)
    fields = {k: v for k, v in changes.items() if k in PLAIN_FIELDS}
    updated = await store.update(connection_id, auth=auth, **fields)
    return ConnectionInfo.from_record(updated)


@router.delete("/{connection_id}", status_code=204, summary="Удалить подключение", responses=ERRORS)
async def delete_connection(connection_id: ConnectionId, store: Connections) -> Response:
    await store.delete(connection_id)
    return Response(status_code=204)


@router.post(
    "/{connection_id}/connect",
    response_model=SSHSessionInfo,
    status_code=201,
    summary="Открыть SSH-сессию по сохранённому подключению",
    description=(
        "Открывает новую сессию, как POST /api/ssh/sessions. Пароль или фразу, которые не "
        "сохранены, передайте в теле. В описании сессии и в событии ssh.session будет "
        "connection_id."
    ),
    responses={
        **ERRORS,
        409: {
            "description": (
                "Ключ сервера не подтверждён (ssh_host_key_unknown) или изменился "
                "(ssh_host_key_mismatch), см. POST /api/ssh/sessions и /api/ssh/known-hosts"
            )
        },
        422: {"description": "Нужен пароль или фраза (connection_secret_required)"},
        429: {"description": "Открыто слишком много сессий (SSH_MAX_SESSIONS)"},
        502: {"description": "Воркер не смог подключиться: хост, ключ или пароль"},
        504: {"description": "Сервер не ответил за SSH_CONNECT_TIMEOUT"},
    },
)
async def connect(
    connection_id: ConnectionId,
    store: Connections,
    keys: Keys,
    manager: Manager,
    request: ConnectionConnectRequest | None = None,
) -> SSHSessionInfo:
    record = await store.get(connection_id)
    request = request or ConnectionConnectRequest()
    password, passphrase = await store.secrets(record)
    if request.password is not None:
        if record.auth != "password":
            raise InvalidConnectionError(f"Connection '{record.name}' does not use a password")
        password = request.password.get_secret_value()
    if request.passphrase is not None:
        if record.auth != "key_path":
            raise InvalidConnectionError(f"Connection '{record.name}' does not use a key file")
        passphrase = request.passphrase.get_secret_value()

    if record.auth == "password" and password is None:
        raise ConnectionSecretRequiredError(
            f"The password for '{record.name}' is not saved: pass it in the request"
        )
    if record.auth == "key_path" and not Path(record.key_path or "").is_file():
        raise InvalidConnectionError(f"Key file of '{record.name}' not found: {record.key_path}")

    params = ConnectParams(
        host=record.host,
        port=record.port,
        username=record.username,
        key_id=record.key_id if record.auth == "key_id" else None,
        key_path=record.key_path if record.auth == "key_path" else None,
        passphrase=passphrase if record.auth == "key_path" else None,
        password=password if record.auth == "password" else None,
    )
    session = await manager.open(params, keys, connection_id=record.id)
    # Подключение могли удалить, пока шло подключение: сессия всё равно открыта
    with contextlib.suppress(ConnectionNotFoundError):
        await store.mark_connected(record.id)
    return SSHSessionInfo(**session.info())


def _new_auth(record: ConnectionRecord, changes: dict[str, Any]) -> ConnectionAuth:
    """Способ входа после PATCH: переданные поля, остальное от текущего способа (если
    метод не меняется) или пусто."""
    method = changes.get("auth", record.auth)
    same = method == record.auth

    def value(field: str) -> Any:
        if field in changes:
            return changes[field]
        return getattr(record, field) if same else None

    def secret(field: str) -> Any:
        if field in changes:
            return _plain(changes[field])
        return KEEP if same else None

    return ConnectionAuth(
        method=method,
        key_id=value("key_id"),
        key_path=value("key_path"),
        password=secret("password"),
        passphrase=secret("passphrase"),
    )


def _plain(secret: Any) -> str | None:
    return secret.get_secret_value() if secret is not None else None
