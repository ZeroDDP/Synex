from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from app.plugins.git.commands import (
    log_args,
    parse_log,
    parse_status,
    quote_path,
    status_args,
)
from app.plugins.git.schemas import (
    PATH_DESCRIPTION,
    CommandResultInfo,
    CommitInfo,
    DeployInfo,
    DeployRequest,
    PullInfo,
    RepoPath,
    RepoRequest,
    RepoStatusInfo,
)
from app.plugins.git.service import PullResult, pull, run_git
from app.plugins.ssh.manager import SSHSessionManager
from app.plugins.ssh.routes import get_ssh_manager
from app.plugins.ssh.schemas import SESSION_ID_PATTERN

router = APIRouter()

Manager = Annotated[SSHSessionManager, Depends(get_ssh_manager)]
SessionId = Annotated[str, Path(pattern=SESSION_ID_PATTERN, description="SSH-сессия на сервер")]
RepoQuery = Annotated[RepoPath, Query(description=PATH_DESCRIPTION)]

ERRORS = {
    404: {"description": "Нет сессии, git, папки или это не репозиторий"},
    409: {"description": "Сессия занята командой пользователя дольше SSH_SERVICE_WAIT"},
    502: {"description": "git завершился с ошибкой или remote недоступен (текст в detail)"},
    504: {"description": "git не уложился в GIT_TIMEOUT"},
}


PULL_ERRORS = {
    **ERRORS,
    409: {
        "description": (
            "pull невозможен: git_local_changes, git_not_fast_forward, git_no_upstream, "
            "git_detached_head; или сессия занята (ssh_session_busy)"
        )
    },
}


@router.get(
    "/sessions/{session_id}/status",
    response_model=RepoStatusInfo,
    summary="Состояние репозитория: ветка, отставание от remote, изменённые файлы",
    responses=ERRORS,
)
async def status(session_id: SessionId, path: RepoQuery, manager: Manager) -> RepoStatusInfo:
    return await _status(manager, session_id, path)


async def _status(manager: SSHSessionManager, session_id: str, path: str) -> RepoStatusInfo:
    result = await run_git(manager, session_id, path, status_args())
    repo = parse_status(result.stdout)
    last = None
    if repo.head is not None:
        commits = parse_log((await run_git(manager, session_id, path, log_args(1))).stdout)
        last = commits[0] if commits else None
    return RepoStatusInfo.build(path, repo, last)


@router.post(
    "/sessions/{session_id}/fetch",
    response_model=RepoStatusInfo,
    summary="Получить изменения с remote без обновления файлов (git fetch)",
    description="После fetch в ответе свежие ahead и behind: сколько коммитов придёт с pull.",
    responses=ERRORS,
)
async def fetch(session_id: SessionId, request: RepoRequest, manager: Manager) -> RepoStatusInfo:
    await run_git(manager, session_id, request.path, ["fetch", "--prune"])
    return await _status(manager, session_id, request.path)


@router.post(
    "/sessions/{session_id}/pull",
    response_model=PullInfo,
    summary="Обновить репозиторий с remote (git pull --ff-only)",
    description=(
        "Только перемотка вперёд: без merge-коммитов и конфликтов. Если на сервере есть "
        "свои изменения или коммиты, ответ 409 с причиной, файлы не меняются."
    ),
    responses=PULL_ERRORS,
)
async def pull_repo(session_id: SessionId, request: RepoRequest, manager: Manager) -> PullInfo:
    return _pull_info(request.path, await pull(manager, session_id, request.path))


@router.post(
    "/sessions/{session_id}/deploy",
    response_model=DeployInfo,
    summary="Деплой: pull и команда в папке репозитория",
    description=(
        "git pull --ff-only (ошибки как у /pull, команда тогда не выполняется), затем "
        "command в папке репозитория с лимитом timeout. Ответ 200 и при ошибке команды: "
        "смотрите ok, exit_code и вывод. Деплой занимает сессию до конца команды: для "
        "него лучше открыть отдельную сессию."
    ),
    responses=PULL_ERRORS,
)
async def deploy(session_id: SessionId, request: DeployRequest, manager: Manager) -> DeployInfo:
    pulled = await pull(manager, session_id, request.path)
    command = None
    if request.command is not None and (pulled.updated or not request.only_if_updated):
        script = f"cd -- {quote_path(request.path)} && {request.command}"
        result = await manager.run_service(
            session_id, script, timeout=request.timeout, name="git deploy"
        )
        command = CommandResultInfo(
            command=request.command,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=result.timed_out,
            duration_ms=result.duration_ms,
        )
    ok = command is None or (command.exit_code == 0 and not command.timed_out)
    return DeployInfo(
        path=request.path, ok=ok, pull=_pull_info(request.path, pulled), command=command
    )


def _pull_info(path: str, result: PullResult) -> PullInfo:
    return PullInfo(
        path=path,
        before=result.before,
        after=result.after,
        updated=result.updated,
        commits=[CommitInfo.from_commit(c) for c in result.commits],
        output=result.output,
    )


@router.get(
    "/sessions/{session_id}/log",
    response_model=list[CommitInfo],
    summary="Последние коммиты текущей ветки",
    responses=ERRORS,
)
async def log(
    session_id: SessionId,
    path: RepoQuery,
    manager: Manager,
    limit: Annotated[int, Query(ge=1, le=500)] = 20,
) -> list[CommitInfo]:
    result = await run_git(manager, session_id, path, log_args(limit))
    return [CommitInfo.from_commit(c) for c in parse_log(result.stdout)]
