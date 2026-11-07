"""Фейковый git для эмулятора shell (FakeShell.programs["git"]).

Повторяет вывод git 2.4x для команд плагина git: `status --porcelain=v2 --branch`,
`log --format=...` (поля через 0x1F), ошибки и коды как у настоящего git. Репозитории
задаются тестом: FakeRepo по пути на сервере.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field

from tests.fake_shell import HOME


@dataclass
class FakeCommit:
    hash: str
    subject: str
    author: str = "Daniil"
    email: str = "dev@example.com"
    date: str = "2026-11-06T12:00:00+07:00"

    def line(self) -> str:
        return "\x1f".join(
            [self.hash, self.hash[:7], self.author, self.email, self.date, self.subject]
        )


@dataclass
class FakeRepo:
    commits: list[FakeCommit] = field(default_factory=list)  # от старых к новым
    branch: str | None = "main"  # None: detached HEAD
    upstream: str | None = "origin/main"
    changes: list[str] = field(default_factory=list)  # строки porcelain v2 как есть
    remote: list[FakeCommit] | None = None  # коммиты upstream на remote (None: как local)
    remote_error: str | None = None  # remote недоступен: текст ошибки git
    blocked_by_changes: bool = False  # pull перезаписал бы локальные изменения
    fetched: list[FakeCommit] | None = None  # upstream на момент последнего fetch

    def __post_init__(self) -> None:
        if self.remote is None:
            self.remote = list(self.commits)
        if self.fetched is None:
            self.fetched = list(self.commits)


@dataclass
class FakeGit:
    repos: dict[str, FakeRepo] = field(default_factory=dict)  # абсолютный путь -> репо
    dirs: set[str] = field(default_factory=lambda: {HOME, "/tmp", "/srv"})
    calls: list[list[str]] = field(default_factory=list)
    envs: list[dict[str, str]] = field(default_factory=list)

    def __call__(self, args: list[str], cwd: str, env: dict[str, str]) -> tuple[str, str, int]:
        self.calls.append(args)
        self.envs.append(env)
        path, args = self._directory(args, cwd)
        if path not in self.dirs and path not in self.repos:
            return "", f"fatal: cannot change to '{path}': No such file or directory\n", 128
        repo = self.repos.get(path)
        if repo is None:
            message = "fatal: not a git repository (or any of the parent directories): .git\n"
            return "", message, 128
        command, rest = args[0], args[1:]
        if command == "status":
            return self._status(repo), "", 0
        if command == "log":
            return self._log(repo, rest)
        if command == "fetch":
            return self._fetch(repo)
        if command == "pull":
            return self._pull(repo)
        return "", f"git: '{command}' is not a git command. See 'git --help'.\n", 1

    def _directory(self, args: list[str], cwd: str) -> tuple[str, list[str]]:
        path = cwd
        rest = list(args)
        while rest and rest[0] in ("-C", "-c"):
            if rest[0] == "-C":
                target = rest[1].replace("$HOME", HOME, 1)
                path = posixpath.normpath(posixpath.join(path, target))
            rest = rest[2:]
        return path, rest

    @staticmethod
    def _status(repo: FakeRepo) -> str:
        head = repo.commits[-1].hash if repo.commits else "(initial)"
        lines = [f"# branch.oid {head}", f"# branch.head {repo.branch or '(detached)'}"]
        if repo.upstream and repo.branch:
            local = {c.hash for c in repo.commits}
            remote = {c.hash for c in repo.fetched or []}
            ahead, behind = len(local - remote), len(remote - local)
            lines += [f"# branch.upstream {repo.upstream}", f"# branch.ab +{ahead} -{behind}"]
        return "\n".join(lines + repo.changes) + "\n"

    @staticmethod
    def _log(repo: FakeRepo, args: list[str]) -> tuple[str, str, int]:
        if not repo.commits:
            branch = repo.branch or "HEAD"
            return "", f"fatal: your current branch '{branch}' does not have any commits yet\n", 128
        limit = len(repo.commits)
        commits = repo.commits
        for arg in args:
            if arg.startswith("--max-count="):
                limit = int(arg.split("=", 1)[1])
            elif ".." in arg:  # before..after: коммиты после before
                before = arg.split("..", 1)[0]
                hashes = [c.hash for c in commits]
                commits = commits[hashes.index(before) + 1 :] if before in hashes else commits
        newest_first = list(reversed(commits))[:limit]
        return "".join(c.line() + "\n" for c in newest_first), "", 0

    @staticmethod
    def _fetch(repo: FakeRepo) -> tuple[str, str, int]:
        if repo.remote_error:
            return "", repo.remote_error, 128
        repo.fetched = list(repo.remote or [])
        return "", "", 0

    @staticmethod
    def _pull(repo: FakeRepo) -> tuple[str, str, int]:
        if repo.branch is None:
            return "", "You are not currently on a branch.\nPlease specify which branch...\n", 1
        if repo.upstream is None:
            return "", "There is no tracking information for the current branch.\n", 1
        if repo.remote_error:
            return "", repo.remote_error, 128
        remote = list(repo.remote or [])
        repo.fetched = list(remote)
        if [c.hash for c in remote[: len(repo.commits)]] != [c.hash for c in repo.commits]:
            return "", "fatal: Not possible to fast-forward, aborting.\n", 128
        if len(remote) == len(repo.commits):
            return "Already up to date.\n", "", 0
        if repo.blocked_by_changes:
            error = (
                "error: Your local changes to the following files would be overwritten by "
                "merge:\n\tconfig.yml\nPlease commit your changes or stash them before you "
                "merge.\nAborting\n"
            )
            return "", error, 1
        before, after = repo.commits[-1].hash[:7], remote[-1].hash[:7]
        repo.commits = remote
        return f"Updating {before}..{after}\nFast-forward\n app.py | 2 +-\n", "", 0
