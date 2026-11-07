"""git на сервере через SSH-сессию: команды как служебные команды сессии
(ssh_manager.run_service: в терминал не попадают) и понятные ошибки по их выводу."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import settings
from app.core.errors import AppError
from app.plugins.git.commands import (
    Commit,
    RepoStatus,
    git_command,
    log_args,
    parse_log,
    parse_status,
    status_args,
)
from app.plugins.git.errors import (
    GitCommandError,
    GitConflictError,
    GitNotARepositoryError,
    GitNotInstalledError,
    GitPathNotFoundError,
    GitRemoteError,
    GitTimeoutError,
)
from app.plugins.ssh.manager import ServiceResult, SSHSessionManager

PULL_LOG_LIMIT = 100  # сколько пришедших коммитов показать после pull


async def run_git(
    manager: SSHSessionManager,
    session_id: str,
    path: str,
    args: list[str],
    *,
    timeout: float | None = None,
) -> ServiceResult:
    """`git -C <path> <args>` в сессии. Ошибку git превращает в ошибку API (classify)."""
    result = await manager.run_service(
        session_id,
        git_command(path, *args),
        timeout=timeout or settings.GIT_TIMEOUT,
        name="git",
    )
    error = classify(result, path)
    if error is not None:
        raise error
    return result


def classify(result: ServiceResult, path: str) -> AppError | None:
    if result.timed_out:
        return GitTimeoutError(
            f"git did not finish within the limit ({result.duration_ms} ms): a slow or "
            "unreachable remote? GIT_TIMEOUT sets the limit"
        )
    if result.exit_code == 0:
        return None
    message = (result.stderr or result.stdout).strip() or f"git exited with {result.exit_code}"
    lower = message.lower()
    if result.exit_code == 127 or "git: not found" in lower or "git: command not found" in lower:
        return GitNotInstalledError(f"git is not installed on the server: {message}")
    if "cannot change to" in lower:
        return GitPathNotFoundError(f"Directory {path} not found on the server: {message}")
    if "not a git repository" in lower:
        return GitNotARepositoryError(f"{path} is not a git repository: {message}")
    for marker, code, hint in _CONFLICTS:
        if marker in lower:
            return GitConflictError(f"{hint}: {message}", code)
    if any(marker in lower for marker in _REMOTE_ERRORS):
        return GitRemoteError(
            f"The remote is not available from the server: {message}. A private repository "
            "must be reachable with the server's SSH key or saved credentials"
        )
    return GitCommandError(message)


# (текст git, код ошибки, пояснение): pull --ff-only не может обновить репозиторий сам
_CONFLICTS = (
    (
        "would be overwritten by",
        "git_local_changes",
        "Local changes on the server would be overwritten: commit or stash them first",
    ),
    (
        "not possible to fast-forward",
        "git_not_fast_forward",
        "The server branch has its own commits: it cannot be fast-forwarded to the remote",
    ),
    (
        "no tracking information",
        "git_no_upstream",
        "The current branch has no upstream branch to pull from",
    ),
    (
        "not currently on a branch",
        "git_detached_head",
        "The repository is in detached HEAD state: check out a branch first",
    ),
)
_REMOTE_ERRORS = (
    "could not read from remote repository",
    "unable to access",
    "authentication failed",
    "could not read username",
    "permission denied (publickey",
    "host key verification failed",
    "repository not found",
)


@dataclass(frozen=True)
class PullResult:
    before: str | None
    after: str | None
    commits: list[Commit]  # пришедшие коммиты, новые первыми
    output: str  # что напечатал git pull

    @property
    def updated(self) -> bool:
        return self.before != self.after


async def repo_status(manager: SSHSessionManager, session_id: str, path: str) -> RepoStatus:
    return parse_status((await run_git(manager, session_id, path, status_args())).stdout)


async def pull(manager: SSHSessionManager, session_id: str, path: str) -> PullResult:
    """git pull --ff-only: только перемотка вперёд, без merge-коммитов и конфликтов."""
    before = (await repo_status(manager, session_id, path)).head
    result = await run_git(manager, session_id, path, ["pull", "--ff-only"])
    after = (await repo_status(manager, session_id, path)).head
    commits: list[Commit] = []
    if before is not None and after is not None and before != after:
        log = await run_git(
            manager, session_id, path, log_args(PULL_LOG_LIMIT, f"{before}..{after}")
        )
        commits = parse_log(log.stdout)
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return PullResult(before, after, commits, output)
