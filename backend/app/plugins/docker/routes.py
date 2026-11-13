from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, WebSocket

from app.core.config import settings
from app.plugins.docker.commands import (
    IMAGES_FORMAT,
    PS_FORMAT,
    STATS_FORMAT,
    parse_containers,
    parse_images,
    parse_stats,
)
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
    LogLineInfo,
)
from app.plugins.docker.service import docker_version, run_docker
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
    status, detail, version = await docker_version(manager, session_id)
    return DockerInfo(
        status=status,
        detail=detail,
        client_version=version.client_version,
        server_version=version.server_version,
        api_version=version.api_version,
        os=version.os,
        arch=version.arch,
    )


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
    args = ["ps", "--no-trunc", "--format", PS_FORMAT]
    if all:
        args.insert(1, "-a")
    result = await run_docker(manager, session_id, args)
    return [ContainerInfo.from_container(c) for c in parse_containers(result.stdout)]


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
        lines=[LogLineInfo.from_line(line) for line in lines],
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
    args: list[str] = [action]
    if timeout is not None and action in ("stop", "restart"):
        args += ["-t", str(timeout)]
    result = await run_docker(manager, session_id, [*args, container], timeout=_limit(timeout))
    return ContainerActionResult(container=container, action=action, output=result.stdout)


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
    args = ["rm"] + (["-f"] if force else []) + (["-v"] if volumes else []) + [container]
    result = await run_docker(manager, session_id, args)
    return ContainerActionResult(container=container, action="remove", output=result.stdout)


@router.get(
    "/sessions/{session_id}/stats",
    response_model=list[ContainerStatsInfo],
    summary="CPU, память, сеть и диск запущенных контейнеров",
    description="Один замер docker stats (занимает пару секунд).",
    responses=ERRORS,
)
async def stats(session_id: SessionId, manager: Manager) -> list[ContainerStatsInfo]:
    args = ["stats", "--no-stream", "--no-trunc", "--format", STATS_FORMAT]
    result = await run_docker(manager, session_id, args)
    return [ContainerStatsInfo.from_stats(s) for s in parse_stats(result.stdout)]


@router.get(
    "/sessions/{session_id}/images",
    response_model=list[ImageInfo],
    summary="Образы на сервере",
    responses=ERRORS,
)
async def images(session_id: SessionId, manager: Manager) -> list[ImageInfo]:
    result = await run_docker(
        manager, session_id, ["images", "--no-trunc", "--format", IMAGES_FORMAT]
    )
    return [ImageInfo.from_image(image) for image in parse_images(result.stdout)]


def _limit(stop_timeout: int | None) -> float | None:
    """stop -t N ждёт контейнер N секунд: лимит команды не меньше N + запас."""
    if stop_timeout is None:
        return None
    return max(settings.DOCKER_TIMEOUT, stop_timeout + 30)
