from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, WebSocket

from app.core.config import settings
from app.plugins.docker.logs import fetch_logs, serve_log_socket
from app.plugins.docker.schemas import (
    CONTAINER_PATTERN,
    SINCE_PATTERN,
    ContainerAction,
    ContainerActionResult,
    ContainerInfo,
    ContainerLogs,
    ContainerStatsInfo,
    DockerInfo,
    ImageInfo,
)
from app.plugins.docker.service import docker_request
from app.plugins.ssh.manager import SSHSessionManager
from app.plugins.ssh.routes import get_ssh_manager
from app.plugins.ssh.schemas import SESSION_ID_PATTERN

router = APIRouter()

Manager = Annotated[SSHSessionManager, Depends(get_ssh_manager)]
SessionId = Annotated[str, Path(pattern=SESSION_ID_PATTERN, description="SSH-сессия на сервер")]
ContainerName = Annotated[str, Path(pattern=CONTAINER_PATTERN, description="Имя или ID контейнера")]

ERRORS = {
    403: {"description": "Пользователь SSH не может обращаться к Docker (группа docker)"},
    404: {"description": "Нет сессии, docker на сервере или контейнера"},
    409: {
        "description": (
            "Сессия занята командой пользователя дольше SSH_SERVICE_WAIT, или действие не "
            "подходит к состоянию контейнера (docker_container_conflict)"
        )
    },
    502: {"description": "docker завершился с ошибкой (текст в detail)"},
    503: {"description": "Демон Docker не запущен или воркер недоступен"},
    504: {"description": "docker не уложился в DOCKER_TIMEOUT"},
}


@router.get(
    "/sessions/{session_id}/info",
    response_model=DockerInfo,
    summary="Есть ли Docker на сервере и его версия",
    responses={404: ERRORS[404], 409: ERRORS[409]},
)
async def info(session_id: SessionId, manager: Manager) -> DockerInfo:
    result = await docker_request(manager, session_id, "docker_info")
    result["status"] = result["status"].removeprefix("docker_")
    return DockerInfo.model_validate(result)


@router.get(
    "/sessions/{session_id}/containers",
    response_model=list[ContainerInfo],
    summary="Контейнеры сервера",
    responses=ERRORS,
)
async def containers(
    session_id: SessionId,
    manager: Manager,
    all: Annotated[bool, Query(description="И остановленные (docker ps -a)")] = True,  # noqa: A002
) -> list[ContainerInfo]:
    return await docker_request(manager, session_id, "docker_containers", {"all": all})


@router.get(
    "/sessions/{session_id}/containers/{container}/logs",
    response_model=ContainerLogs,
    summary="Логи контейнера",
    description=(
        "Последние `tail` строк stdout и stderr контейнера по времени. Для новых строк "
        "передайте `since` = `next_since` предыдущего ответа."
    ),
    responses=ERRORS,
)
async def container_logs(
    session_id: SessionId,
    container: ContainerName,
    manager: Manager,
    tail: Annotated[int, Query(ge=0, le=10000, description="Сколько последних строк")] = 200,
    since: Annotated[
        str | None,
        Query(
            pattern=SINCE_PATTERN,
            description="С какого времени: next_since, RFC3339, Unix-время или 10m, 1h",
        ),
    ] = None,
) -> ContainerLogs:
    lines, next_since = await fetch_logs(manager, session_id, container, tail=tail, since=since)
    return ContainerLogs(
        container=container,
        lines=lines,
        next_since=next_since,
    )


@router.websocket("/sessions/{session_id}/containers/{container}/logs/ws")
async def container_logs_stream(
    websocket: WebSocket,
    session_id: SessionId,
    container: ContainerName,
    manager: Manager,
    tail: Annotated[int, Query(ge=0, le=10000)] = 200,
    since: Annotated[str | None, Query(pattern=SINCE_PATTERN)] = None,
) -> None:
    """Живой лог контейнера: последние `tail` строк и новые по мере появления.
    Протокол в app/plugins/docker/logs.py."""
    await serve_log_socket(websocket, manager, session_id, container, tail, since)


@router.post(
    "/sessions/{session_id}/containers/{container}/{action}",
    response_model=ContainerActionResult,
    summary="Запустить, остановить, перезапустить, приостановить или убить контейнер",
    responses=ERRORS,
)
async def container_action(
    session_id: SessionId,
    container: ContainerName,
    action: ContainerAction,
    manager: Manager,
    timeout: Annotated[
        int | None,
        Query(ge=0, le=3600, description="stop и restart: сколько секунд ждать до KILL"),
    ] = None,
) -> ContainerActionResult:
    worker_container = container
    if re.fullmatch(r"[0-9a-f]{1,63}", container):
        # Core looks up the resulting state by full ID or name after applying the action.
        rows = await docker_request(manager, session_id, "docker_containers", {"all": True})
        named = [row for row in rows if container in row["names"] or container == row["name"]]
        matches = named or [row for row in rows if row["id"].startswith(container)]
        if len(matches) == 1:
            worker_container = matches[0]["id"]
    result = await docker_request(
        manager,
        session_id,
        "docker_container_action",
        {"container": worker_container, "action": action, "timeout": timeout},
        timeout=_limit(timeout),
    )
    return ContainerActionResult(container=container, action=action, output=result["output"])


@router.delete(
    "/sessions/{session_id}/containers/{container}",
    response_model=ContainerActionResult,
    summary="Удалить контейнер",
    responses=ERRORS,
)
async def remove_container(
    session_id: SessionId,
    container: ContainerName,
    manager: Manager,
    force: Annotated[bool, Query(description="Удалить и запущенный (docker rm -f)")] = False,
    volumes: Annotated[bool, Query(description="Вместе с анонимными томами (-v)")] = False,
) -> ContainerActionResult:
    await docker_request(
        manager,
        session_id,
        "docker_container_remove",
        {"container": container, "force": force, "volumes": volumes},
    )
    return ContainerActionResult(container=container, action="remove", output=container)


@router.get(
    "/sessions/{session_id}/stats",
    response_model=list[ContainerStatsInfo],
    summary="CPU, память, сеть и диск запущенных контейнеров",
    description="Один замер docker stats (занимает пару секунд).",
    responses=ERRORS,
)
async def stats(session_id: SessionId, manager: Manager) -> list[ContainerStatsInfo]:
    samples = await docker_request(manager, session_id, "docker_stats")
    for sample in samples:
        sample["name"] = sample["name"].removeprefix("/")
    return samples


@router.get(
    "/sessions/{session_id}/images",
    response_model=list[ImageInfo],
    summary="Образы на сервере",
    responses=ERRORS,
)
async def images(session_id: SessionId, manager: Manager) -> list[ImageInfo]:
    return await docker_request(manager, session_id, "docker_images")


def _limit(stop_timeout: int | None) -> float | None:
    """stop -t N ждёт контейнер N секунд: лимит команды не меньше N + запас."""
    if stop_timeout is None:
        return None
    return max(settings.DOCKER_TIMEOUT, stop_timeout + 30)
