"""Плагин git: разбор вывода git и API через SSH-сессию (фейковый и настоящий git)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.plugins.git.commands import (
    FileChange,
    parse_log,
    parse_status,
    quote_path,
    unquote,
)
from tests.conftest import needs_unicode_file_names
from tests.fake_git import FakeCommit, FakeGit, FakeRepo
from tests.fake_shell import HOME
from tests.plugin_rig import plugin_rig

# Вывод настоящего git 2.43: status --porcelain=v2 --branch (с core.quotePath=false)
STATUS_OUTPUT = """# branch.oid 9cf2e916a8d89327dfb0d3d58de2736dc96dd7f7
# branch.head main
# branch.upstream origin/main
# branch.ab +2 -5
2 RM N... 100644 100644 100644 7898192 7898192 R100 b.txt\ta.txt
1 A. N... 000000 100644 100644 0000000 587be6b ünï.txt
1 .M N... 100644 100644 100644 1111111 1111111 dir/with space.py
u UU N... 100644 100644 100644 100644 aaaaaaa bbbbbbb ccccccc conflict.txt
? new file.txt
? "we\\"ird\\\\name.txt"
"""
LOG_LINE = "\x1f".join(
    [
        "9cf2e916a8d89327dfb0d3d58de2736dc96dd7f7",
        "9cf2e91",
        "Тест Юзер",
        "a@b",
        "2026-11-06T23:12:14+07:00",
        "first: a|b",
    ]
)


def test_parse_status():
    status = parse_status(STATUS_OUTPUT)
    assert (status.head, status.branch, status.upstream) == (
        "9cf2e916a8d89327dfb0d3d58de2736dc96dd7f7",
        "main",
        "origin/main",
    )
    assert (status.ahead, status.behind) == (2, 5)
    assert status.changes == [
        FileChange("b.txt", "renamed", "R", "M", "a.txt"),
        FileChange("ünï.txt", "changed", "A", "."),
        FileChange("dir/with space.py", "changed", ".", "M"),
        FileChange("conflict.txt", "conflict", "U", "U"),
        FileChange("new file.txt", "untracked", "?", "?"),
        FileChange('we"ird\\name.txt', "untracked", "?", "?"),
    ]


def test_parse_status_of_empty_and_detached_repo():
    empty = parse_status("# branch.oid (initial)\n# branch.head master\n")
    assert (empty.head, empty.branch, empty.upstream, empty.changes) == (None, "master", None, [])
    detached = parse_status("# branch.oid 9cf2e91\n# branch.head (detached)\n")
    assert (detached.head, detached.branch) == ("9cf2e91", None)


def test_unquote_c_style_paths():
    assert unquote("plain.txt") == "plain.txt"
    assert unquote('"tab\\there"') == "tab\there"
    assert unquote('"\\303\\274ber.txt"') == "über.txt"  # без core.quotePath=false


def test_parse_log():
    (commit,) = parse_log(LOG_LINE + "\n\ngarbage\n")
    assert (commit.short_hash, commit.author_name, commit.subject) == (
        "9cf2e91",
        "Тест Юзер",
        "first: a|b",
    )
    assert commit.date == datetime(2026, 11, 6, 23, 12, 14, tzinfo=timezone(timedelta(hours=7)))


@pytest.mark.parametrize(
    ("path", "quoted"),
    [
        ("/srv/app", "/srv/app"),
        ("/srv/my app", "'/srv/my app'"),
        ("~", '"$HOME"'),
        ("~/", '"$HOME"'),
        ("~/projects/shop", '"$HOME"/projects/shop'),
        ("~/a b; rm -rf ~", "\"$HOME\"/'a b; rm -rf ~'"),
        ("app", "app"),
    ],
)
def test_quote_path(path, quoted):
    assert quote_path(path) == quoted


# ------------------------------------------------------------ API (fake git)

COMMITS = [
    FakeCommit("1111111aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "Initial commit"),
    FakeCommit("2222222bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "Add API"),
    FakeCommit("3333333ccccccccccccccccccccccccccccccccc", "Fix deploy script"),
]


@pytest.fixture
def git() -> FakeGit:
    return FakeGit(
        repos={
            f"{HOME}/app": FakeRepo(commits=list(COMMITS)),
            "/srv/dirty": FakeRepo(
                commits=list(COMMITS[:1]),
                changes=["1 .M N... 100644 100644 100644 1 1 config.yml", "? notes.txt"],
            ),
            "/srv/empty": FakeRepo(),
            "/srv/detached": FakeRepo(commits=list(COMMITS), branch=None),
        }
    )


@pytest.fixture
async def rig(tmp_path: Path, missing_socket_path, git):
    async with plugin_rig(tmp_path, missing_socket_path, ["git"], {"git": git}) as rig:
        yield rig


async def test_status(rig, git):
    url = f"/api/git/{rig.session_url}/status"
    response = await rig.api.get(url, params={"path": "~/app"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["path"] == "~/app"
    assert (body["head"], body["branch"], body["detached"]) == (COMMITS[2].hash, "main", False)
    assert (body["upstream"], body["ahead"], body["behind"]) == ("origin/main", 0, 0)
    assert (body["clean"], body["changes"]) == (True, [])
    assert body["last_commit"]["subject"] == "Fix deploy script"
    assert git.calls[0][:4] == ["-C", "$HOME/app", "-c", "core.quotePath=false"]
    assert git.envs[0] == {"LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"}

    dirty = (await rig.api.get(url, params={"path": "/srv/dirty"})).json()
    assert dirty["clean"] is False
    assert [(c["path"], c["kind"]) for c in dirty["changes"]] == [
        ("config.yml", "changed"),
        ("notes.txt", "untracked"),
    ]
    empty = (await rig.api.get(url, params={"path": "/srv/empty"})).json()
    assert (empty["head"], empty["last_commit"]) == (None, None)
    detached = (await rig.api.get(url, params={"path": "/srv/detached"})).json()
    assert (detached["branch"], detached["detached"], detached["upstream"]) == (None, True, None)
    # Служебные команды в терминал сессии не попадают
    session = rig.manager.get(rig.session_id)
    assert [e.kind for e in session.buffer.since(0).entries] == ["opened"]


async def test_log(rig):
    url = f"/api/git/{rig.session_url}/log"
    commits = (await rig.api.get(url, params={"path": f"{HOME}/app", "limit": 2})).json()
    assert [c["subject"] for c in commits] == ["Fix deploy script", "Add API"]
    assert set(commits[0]) == {
        "hash", "short_hash", "author_name", "author_email", "date", "subject",
    }  # fmt: skip
    empty = await rig.api.get(url, params={"path": "/srv/empty"})
    assert empty.status_code == 502
    assert "does not have any commits yet" in empty.json()["detail"]


@pytest.mark.parametrize(
    ("path", "status", "error"),
    [
        ("/srv/missing", 404, "git_path_not_found"),
        ("/tmp", 404, "git_not_a_repository"),
    ],
)
async def test_errors(rig, path, status, error):
    response = await rig.api.get(f"/api/git/{rig.session_url}/status", params={"path": path})
    assert response.status_code == status, response.text
    assert response.json()["error"] == error


async def test_git_not_installed_and_bad_path(rig):
    url = f"/api/git/{rig.session_url}/status"
    assert (await rig.api.get(url, params={"path": "a\nb"})).status_code == 422
    assert (await rig.api.get(url)).status_code == 422  # path обязателен
    rig.worker.shell.programs.pop("git")
    response = await rig.api.get(url, params={"path": "~/app"})
    assert response.status_code == 404
    assert response.json()["error"] == "git_not_installed"


NEW_COMMITS = [
    FakeCommit("4444444ddddddddddddddddddddddddddddddddd", "Add healthcheck"),
    FakeCommit("5555555eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee", "Bump version"),
]
SSH_REMOTE_ERROR = (
    "git@github.com: Permission denied (publickey).\n"
    "fatal: Could not read from remote repository.\n"
)


async def test_fetch_shows_incoming_commits(rig, git):
    git.repos[f"{HOME}/app"].remote = COMMITS + NEW_COMMITS
    before = (await rig.api.get(f"/api/git/{rig.session_url}/status?path=~/app")).json()
    assert before["behind"] == 0  # remote ещё не опрашивали

    response = await rig.api.post(f"/api/git/{rig.session_url}/fetch", json={"path": "~/app"})
    assert response.status_code == 200, response.text
    assert (response.json()["behind"], response.json()["ahead"]) == (2, 0)
    assert ["fetch", "--prune"] in [call[2:] for call in git.calls]


async def test_pull(rig, git):
    git.repos[f"{HOME}/app"].remote = COMMITS + NEW_COMMITS
    url = f"/api/git/{rig.session_url}/pull"
    response = await rig.api.post(url, json={"path": "~/app"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["before"], body["after"], body["updated"]) == (
        COMMITS[-1].hash,
        NEW_COMMITS[-1].hash,
        True,
    )
    assert [c["subject"] for c in body["commits"]] == ["Bump version", "Add healthcheck"]
    assert "Fast-forward" in body["output"]
    assert ["pull", "--ff-only"] in [call[2:] for call in git.calls]

    again = (await rig.api.post(url, json={"path": "~/app"})).json()
    assert (again["updated"], again["commits"], again["output"]) == (
        False,
        [],
        "Already up to date.",
    )


@pytest.mark.parametrize(
    ("repo", "status", "error"),
    [
        (FakeRepo(commits=COMMITS[:1], remote=COMMITS[1:]), 409, "git_not_fast_forward"),
        (
            FakeRepo(commits=COMMITS[:1], remote=COMMITS, blocked_by_changes=True),
            409,
            "git_local_changes",
        ),
        (FakeRepo(commits=COMMITS, upstream=None), 409, "git_no_upstream"),
        (FakeRepo(commits=COMMITS, branch=None), 409, "git_detached_head"),
        (FakeRepo(commits=COMMITS, remote_error=SSH_REMOTE_ERROR), 502, "git_remote_unavailable"),
    ],
)
async def test_pull_problems(rig, git, repo, status, error):
    git.repos["/srv/repo"] = repo
    head = repo.commits[-1].hash
    response = await rig.api.post(f"/api/git/{rig.session_url}/pull", json={"path": "/srv/repo"})
    assert response.status_code == status, response.text
    assert response.json()["error"] == error
    assert repo.commits[-1].hash == head  # файлы не изменились


@pytest.fixture
def app_dir(rig) -> Path:
    """Папка ~/app в виртуальной ФС эмулятора shell (для cd перед командой деплоя)."""
    path = rig.worker.shell.root / HOME.lstrip("/") / "app"
    path.mkdir(parents=True)
    return path


async def test_deploy_runs_command_after_pull(rig, git, app_dir):
    git.repos[f"{HOME}/app"].remote = COMMITS + NEW_COMMITS
    response = await rig.api.post(
        f"/api/git/{rig.session_url}/deploy",
        json={"path": "~/app", "command": "pwd && echo restarted >&2", "timeout": 30},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["pull"]["updated"] is True
    assert body["command"] == {
        "command": "pwd && echo restarted >&2",
        "exit_code": 0,
        "stdout": f"{HOME}/app",  # команда выполняется в папке репозитория
        "stderr": "restarted",
        "timed_out": False,
        "duration_ms": body["command"]["duration_ms"],
    }
    session = rig.manager.get(rig.session_id)
    assert [e.kind for e in session.buffer.since(0).entries] == ["opened"]


async def test_deploy_without_changes_and_failures(rig, git, app_dir):
    url = f"/api/git/{rig.session_url}/deploy"
    skipped = (
        await rig.api.post(url, json={"path": "~/app", "command": "pwd", "only_if_updated": True})
    ).json()
    assert (skipped["ok"], skipped["pull"]["updated"], skipped["command"]) == (True, False, None)

    forced = (await rig.api.post(url, json={"path": "~/app", "command": "pwd"})).json()
    assert forced["command"]["stdout"] == f"{HOME}/app"  # без only_if_updated выполняется

    failed = (await rig.api.post(url, json={"path": "~/app", "command": "false"})).json()
    assert (failed["ok"], failed["command"]["exit_code"]) == (False, 1)

    only_pull = (await rig.api.post(url, json={"path": "~/app"})).json()
    assert (only_pull["ok"], only_pull["command"]) == (True, None)

    # pull не прошёл: команда не выполняется
    git.repos[f"{HOME}/app"].remote_error = SSH_REMOTE_ERROR
    calls = len(git.calls)
    blocked = await rig.api.post(url, json={"path": "~/app", "command": "echo never"})
    assert blocked.status_code == 502
    assert blocked.json()["error"] == "git_remote_unavailable"
    assert len(git.calls) == calls + 2  # status и pull, команда деплоя не запускалась
    assert (await rig.api.post(url, json={"path": "~/app", "timeout": 0})).status_code == 422


# ------------------------------------------------------------ API (real git)

needs_real_git = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("git") or not shutil.which("sh"),
    reason="needs sh and git",
)


def run_git(*args: str, cwd: Path, home: Path) -> str:
    env = {**os.environ, "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1"}
    env.pop("GIT_DIR", None)
    identity = ["-c", "user.name=Test", "-c", "user.email=t@e", "-c", "commit.gpgsign=false"]
    result = subprocess.run(
        ["git", *identity, *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return result.stdout


@pytest.fixture
def real_repo(tmp_path: Path) -> Path:
    """Репозиторий ~/app (домашняя папка фейкового сервера) с origin в ~/origin.git."""
    home = tmp_path / "home"
    home.mkdir()
    run_git("init", "-q", "--bare", "-b", "main", "origin.git", cwd=home, home=home)
    run_git("clone", "-q", "origin.git", "app", cwd=home, home=home)
    app = home / "app"
    (app / "readme.md").write_text("hello\n")
    run_git("add", ".", cwd=app, home=home)
    run_git("commit", "-q", "-m", "Initial commit", cwd=app, home=home)
    run_git("push", "-q", "origin", "HEAD:main", cwd=app, home=home)
    run_git("branch", "-q", "-u", "origin/main", cwd=app, home=home)
    return app


@needs_real_git
@needs_unicode_file_names
async def test_status_and_log_with_real_git(tmp_path: Path, missing_socket_path, real_repo):
    (real_repo / "readme.md").write_text("changed\n")
    (real_repo / "новый файл.txt").write_text("x")
    async with plugin_rig(tmp_path, missing_socket_path, ["git"]) as rig:
        status = await rig.api.get(f"/api/git/{rig.session_url}/status", params={"path": "~/app"})
        assert status.status_code == 200, status.text
        body = status.json()
        assert (body["branch"], body["upstream"], body["ahead"], body["behind"]) == (
            "main",
            "origin/main",
            0,
            0,
        )
        assert [(c["path"], c["kind"]) for c in body["changes"]] == [
            ("readme.md", "changed"),
            ("новый файл.txt", "untracked"),
        ]
        assert body["last_commit"]["subject"] == "Initial commit"

        log = (await rig.api.get(f"/api/git/{rig.session_url}/log", params={"path": "app"})).json()
        assert [c["subject"] for c in log] == ["Initial commit"]

        missing = await rig.api.get(f"/api/git/{rig.session_url}/status", params={"path": "~/nope"})
        assert missing.json()["error"] == "git_path_not_found"


@needs_real_git
async def test_fetch_and_pull_with_real_git(tmp_path: Path, missing_socket_path, real_repo):
    home = real_repo.parent
    # Кто-то запушил в origin два коммита
    run_git("clone", "-q", "origin.git", "other", cwd=home, home=home)
    other = home / "other"
    for name in ("one", "two"):
        (other / name).write_text(name)
        run_git("add", name, cwd=other, home=home)
        run_git("commit", "-q", "-m", f"Add {name}", cwd=other, home=home)
    run_git("push", "-q", "origin", "HEAD:main", cwd=other, home=home)

    async with plugin_rig(tmp_path, missing_socket_path, ["git"]) as rig:
        base = f"/api/git/{rig.session_url}"
        fetched = (await rig.api.post(f"{base}/fetch", json={"path": "~/app"})).json()
        assert (fetched["behind"], fetched["ahead"]) == (2, 0)

        pulled = await rig.api.post(f"{base}/pull", json={"path": "~/app"})
        assert pulled.status_code == 200, pulled.text
        body = pulled.json()
        assert body["updated"] is True
        assert [c["subject"] for c in body["commits"]] == ["Add two", "Add one"]
        assert (real_repo / "two").read_text() == "two"

        # Своя правка на сервере в файле, который меняет следующий коммит
        (other / "one").write_text("remote change")
        run_git("commit", "-q", "-am", "Change one", cwd=other, home=home)
        run_git("push", "-q", "origin", "HEAD:main", cwd=other, home=home)
        (real_repo / "one").write_text("local change")
        blocked = await rig.api.post(f"{base}/pull", json={"path": "~/app"})
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["error"] == "git_local_changes"


@needs_real_git
async def test_deploy_with_real_git(tmp_path: Path, missing_socket_path, real_repo):
    async with plugin_rig(tmp_path, missing_socket_path, ["git"]) as rig:
        response = await rig.api.post(
            f"/api/git/{rig.session_url}/deploy",
            json={"path": "~/app", "command": "git log -1 --format=%s && pwd"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        subject, cwd = body["command"]["stdout"].splitlines()
        assert subject == "Initial commit"
        assert Path(cwd).resolve() == real_repo.resolve()
