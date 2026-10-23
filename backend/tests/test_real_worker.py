"""Smoke-тесты с настоящим synex_worker.exe.

Запускаются только на Windows, если бинарник лежит по пути из настроек.
SSH-сервер для первого теста не нужен: проверяются запуск, pipe, фрейминг и MessagePack.

Полный SSH-сценарий включается переменными окружения:
    SYNEX_TEST_SSH_HOST, SYNEX_TEST_SSH_USER, SYNEX_TEST_SSH_KEY, [SYNEX_TEST_SSH_PORT]
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx
import pytest

from app.core.app import create_app
from app.core.config import settings
from app.core.errors import WorkerCommandError
from app.plugins.ssh.session import ssh_state
from app.plugins.ssh.wrapper import build_command, new_marker, parse_output
from app.worker.manager import WorkerManager
from tests.conftest import unique_pipe_name

EXE = next((p for p in WorkerManager.executable_candidates() if p.is_file()), None)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or EXE is None,
    reason="needs Windows and synex_worker.exe",
)


@pytest.fixture
async def real_worker(monkeypatch, free_port):
    # Своё имя pipe и порт, чтобы не столкнуться с запущенным dev-бэкендом
    monkeypatch.setattr(settings, "RUST_IPC_PIPE_NAME", unique_pipe_name())
    monkeypatch.setattr(settings, "RUST_IPC_TCP_FALLBACK_PORT", free_port)
    monkeypatch.setattr(settings, "RUST_IPC_REQUEST_TIMEOUT", 10.0)
    manager = WorkerManager()
    await manager.start()
    yield manager
    await manager.stop()


async def test_real_worker_pipe_roundtrip(real_worker):
    status = real_worker.status()
    assert status["connected"], real_worker.worker_output_tail
    assert status["transport"] == "pipe", real_worker.worker_output_tail

    client = real_worker.client
    with pytest.raises(WorkerCommandError, match="Not connected"):
        await client.request("ssh_exec", {"command": "whoami"})

    with pytest.raises(WorkerCommandError, match="Unknown action"):
        await client.request("sftp_list_dir", {"path": "/"}, timeout=5)

    # Несколько запросов одновременно по одному pipe
    results = await asyncio.gather(
        *[client.request("ssh_exec", {"command": f"echo {i}"}) for i in range(10)],
        return_exceptions=True,
    )
    assert all(isinstance(r, WorkerCommandError) for r in results), results
    assert real_worker.status()["connected"]


@pytest.fixture
async def ssh_client(real_worker):
    if not os.environ.get("SYNEX_TEST_SSH_HOST"):
        pytest.skip("SYNEX_TEST_SSH_* not set")
    client = real_worker.client
    connected = await client.request(
        "ssh_connect",
        {
            "connection": {
                "host": os.environ["SYNEX_TEST_SSH_HOST"],
                "port": int(os.environ.get("SYNEX_TEST_SSH_PORT", "22")),
                "username": os.environ["SYNEX_TEST_SSH_USER"],
                "key_path": os.environ["SYNEX_TEST_SSH_KEY"],
            }
        },
        timeout=30,
    )
    assert connected["exit_code"] == 0
    return client


async def test_real_ssh_session_with_cwd(ssh_client):
    marker = new_marker()
    cwd: str | None = None

    async def run(command: str) -> dict:
        nonlocal cwd
        result = await ssh_client.request(
            "ssh_exec", {"command": build_command(command, cwd, marker, timeout=20)}, timeout=40
        )
        parsed = parse_output(result["stdout"], marker, timeout_applied=True)
        cwd = parsed.cwd or cwd
        result["stdout"] = parsed.stdout
        if parsed.exit_code is not None:
            result["exit_code"] = parsed.exit_code  # так же делает плагин SSH
        return result

    home = (await run("pwd"))["stdout"]
    assert home.startswith("/") and cwd == home

    await run("cd /tmp")
    assert cwd == "/tmp"
    assert (await run("pwd"))["stdout"] == "/tmp"
    assert (await run("false"))["exit_code"] == 1
    assert (await run("ls /definitely/missing"))["exit_code"] == 2
    assert (await run("echo hi; exit 0"))["exit_code"] == 0
    assert cwd == "/tmp"


async def test_real_worker_raw_exit_codes(ssh_client):
    """Коды возврата от самого воркера, без обёртки Python.

    Падает, пока воркер теряет exit-status (на 2026-10-22 он отдаёт 0 для `false`).
    Вывод теста показывает, на каких командах код теряется.
    """
    cases = {
        "true": 0,
        "false": 1,
        "exit 3": 3,
        "ls /definitely/missing": 2,
        "echo out; exit 5": 5,
        "sleep 1; exit 4": 4,
    }
    got = {}
    for command in cases:
        result = await ssh_client.request("ssh_exec", {"command": command}, timeout=30)
        got[command] = result["exit_code"]

    report = "\n".join(
        f"  {'OK ' if got[c] == want else 'BAD'} {c!r}: expected {want}, worker returned {got[c]}"
        for c, want in cases.items()
    )
    assert got == cases, f"synex_worker returns wrong exit codes:\n{report}"


async def test_real_api_ssh_connect(monkeypatch, free_port):
    """Всё приложение целиком: HTTP → плагин ssh → synex_worker.exe → сервер."""
    if not os.environ.get("SYNEX_TEST_SSH_HOST"):
        pytest.skip("SYNEX_TEST_SSH_* not set")
    monkeypatch.setattr(settings, "RUST_IPC_PIPE_NAME", unique_pipe_name())
    monkeypatch.setattr(settings, "RUST_IPC_TCP_FALLBACK_PORT", free_port)

    app = create_app()
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=60
            ) as api:
                response = await api.post(
                    "/api/ssh/connect",
                    json={
                        "host": os.environ["SYNEX_TEST_SSH_HOST"],
                        "port": int(os.environ.get("SYNEX_TEST_SSH_PORT", "22")),
                        "username": os.environ["SYNEX_TEST_SSH_USER"],
                        "key_path": os.environ["SYNEX_TEST_SSH_KEY"],
                    },
                )
                assert response.status_code == 201, response.text

                async def run(command: str) -> dict:
                    reply = await api.post("/api/ssh/exec", json={"command": command})
                    assert reply.status_code == 200, reply.text
                    return reply.json()

                assert (await run("cd /tmp"))["cwd"] == "/tmp"
                assert (await run("pwd"))["stdout"] == "/tmp"
                assert (await run("false"))["exit_code"] == 1

                # Таймаут на сервере: команда прервана, воркер сразу свободен
                stopped = await api.post(
                    "/api/ssh/exec", json={"command": "echo started; sleep 60", "timeout": 2}
                )
                assert stopped.status_code == 200, stopped.text
                body = stopped.json()
                assert body["timed_out"] is True, body
                assert body["stdout"] == "started"
                assert body["duration_ms"] < 15_000
                assert (await run("echo next"))["stdout"] == "next"

                # Тот же сервер через ключ из хранилища (временная копия, тестовый мастер-ключ)
                imported = await api.post(
                    "/api/keys",
                    json={"name": "test", "private_key_path": os.environ["SYNEX_TEST_SSH_KEY"]},
                )
                assert imported.status_code == 201, imported.text
                by_id = await api.post(
                    "/api/ssh/connect",
                    json={
                        "host": os.environ["SYNEX_TEST_SSH_HOST"],
                        "port": int(os.environ.get("SYNEX_TEST_SSH_PORT", "22")),
                        "username": os.environ["SYNEX_TEST_SSH_USER"],
                        "key_id": imported.json()["id"],
                    },
                )
                assert by_id.status_code == 201, by_id.text
                assert (await run("echo via-keystore"))["stdout"] == "via-keystore"
    finally:
        ssh_state.mark_disconnected()
