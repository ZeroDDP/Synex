from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field

from app.plugins.git.commands import Commit, FileChange, RepoStatus


def _check_path(value: str) -> str:
    if any(ord(ch) < 32 for ch in value):
        raise ValueError("must not contain control characters")
    return value


RepoPath = Annotated[str, Field(min_length=1, max_length=1024), AfterValidator(_check_path)]
PATH_DESCRIPTION = "Папка репозитория на сервере: абсолютный путь, ~/app или путь от домашней папки"


class CommitInfo(BaseModel):
    hash: str
    short_hash: str
    author_name: str
    author_email: str
    date: datetime | None
    subject: str = Field(..., description="Первая строка сообщения коммита")

    @classmethod
    def from_commit(cls, commit: Commit) -> CommitInfo:
        return cls(**commit.__dict__)


class FileChangeInfo(BaseModel):
    path: str
    kind: str = Field(..., description="changed, renamed, conflict, untracked")
    index: str = Field(..., description='В индексе: M, A, D, R, C, T, U или "." (нет)')
    worktree: str = Field(..., description="В рабочей папке: те же буквы")
    orig_path: str | None = Field(default=None, description="Откуда переименован")

    @classmethod
    def from_change(cls, change: FileChange) -> FileChangeInfo:
        return cls(**change.__dict__)


class RepoStatusInfo(BaseModel):
    path: str
    head: str | None = Field(..., description="Текущий коммит; null: коммитов ещё нет")
    branch: str | None = Field(..., description="null: detached HEAD")
    detached: bool
    upstream: str | None = Field(..., description="Ветка на remote, например origin/main")
    ahead: int | None = Field(..., description="Своих коммитов, которых нет на remote")
    behind: int | None = Field(
        ..., description="Коммитов на remote, которых нет здесь (по последнему fetch)"
    )
    clean: bool = Field(..., description="Нет изменённых и новых файлов")
    changes: list[FileChangeInfo]
    last_commit: CommitInfo | None

    @classmethod
    def build(cls, path: str, status: RepoStatus, last: Commit | None) -> RepoStatusInfo:
        return cls(
            path=path,
            head=status.head,
            branch=status.branch,
            detached=status.head is not None and status.branch is None,
            upstream=status.upstream,
            ahead=status.ahead,
            behind=status.behind,
            clean=not status.changes,
            changes=[FileChangeInfo.from_change(c) for c in status.changes],
            last_commit=CommitInfo.from_commit(last) if last else None,
        )


class RepoRequest(BaseModel):
    path: RepoPath = Field(..., description=PATH_DESCRIPTION)


class PullInfo(BaseModel):
    path: str
    before: str | None = Field(..., description="Коммит до pull")
    after: str | None = Field(..., description="Коммит после pull")
    updated: bool = Field(..., description="Пришли новые коммиты")
    commits: list[CommitInfo] = Field(..., description="Пришедшие коммиты, новые первыми")
    output: str = Field(..., description="Что напечатал git pull")


class DeployRequest(BaseModel):
    path: RepoPath = Field(..., description=PATH_DESCRIPTION)
    command: str | None = Field(
        default=None,
        min_length=1,
        max_length=4000,
        description=(
            "Что выполнить в папке репозитория после pull, например "
            "`docker compose up -d --build`. Без команды деплой это только pull"
        ),
    )
    only_if_updated: bool = Field(
        default=False, description="Выполнять команду, только если pull принёс коммиты"
    )
    timeout: int = Field(default=600, ge=1, le=3600, description="Лимит команды, сек")


class CommandResultInfo(BaseModel):
    command: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = Field(..., description="Команду прервал лимит timeout")
    duration_ms: int


class DeployInfo(BaseModel):
    path: str
    ok: bool = Field(..., description="pull прошёл и команда (если была) завершилась с кодом 0")
    pull: PullInfo
    command: CommandResultInfo | None = Field(
        ..., description="null: команды нет или она пропущена (only_if_updated)"
    )
