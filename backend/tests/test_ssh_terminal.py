"""Терминал поверх команд сессии: прерывание (Ctrl+C), защита от программ, которым нужен
терминал, и лимит времени команд терминала.

Прерывание идёт в настоящем sh (Linux, macOS): сигналы получают настоящие процессы.
Остальное идёт где угодно.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

from app.core.config import settings
from app.plugins.ssh.terminal import interactive_program
from tests.conftest import needs_timeout_utility
from tests.plugin_rig import PluginRig, plugin_rig
from tests.ssh_helpers import recv_json, send_json, serve

needs_sh = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("sh"), reason="needs a real sh"
)


@pytest.fixture
async def shell_rig(tmp_path: Path, missing_socket_path) -> AsyncIterator[PluginRig]:
    """Сессия с настоящим sh: команды выполняются на этой машине, home — папка теста."""
    async with plugin_rig(tmp_path, missing_socket_path, ["ssh"]) as rig:
        yield rig


@pytest.fixture
async def rig(tmp_path: Path, missing_socket_path) -> AsyncIterator[PluginRig]:
    """Сессия с эмулятором shell: идёт на любой ОС."""
    async with plugin_rig(tmp_path, missing_socket_path, ["ssh"], {}) as r:
        yield r


def exec_url(rig: PluginRig) -> str:
    return f"/api/ssh/sessions/{rig.session_id}/exec"


async def wait_busy(rig: PluginRig) -> None:
    async with asyncio.timeout(5):
        while not rig.manager.get(rig.session_id).running_marker:
            await asyncio.sleep(0.02)
    await asyncio.sleep(0.3)  # команда успела запуститься на «сервере»


# ------------------------------------------------------------- interactive


@pytest.mark.parametrize(
    ("command", "program"),
    [
        ("vim /etc/hosts", "vim"),
        ("sudo -u www nano x", "nano"),
        ("cd /tmp && top", "top"),
        ("ls\nhtop", "htop"),
        ("git commit", "git"),
        ("git rebase -i HEAD~2", "git"),
        ("crontab -e", "crontab"),
        ("FOO=1 /usr/bin/tmux attach", "tmux"),
    ],
)
def test_programs_that_need_a_terminal(command: str, program: str):
    found = interactive_program(command)
    assert found is not None and found.program == program


@pytest.mark.parametrize(
    "command",
    [
        "top -b -n 1",
        "ls | less",
        "echo 'vim; top'",
        "git commit -m 'fix'",
        "git commit --amend --no-edit",
        "crontab -l",
        "grep top file",
        "python3 script.py",
        "emacs --batch -l build.el",
    ],
)
def test_commands_that_work_without_a_terminal(command: str):
    assert interactive_program(command) is None


async def test_interactive_command_is_refused_with_a_hint(rig: PluginRig):
    response = await rig.api.post(exec_url(rig), json={"command": "vim notes.txt"})
    assert response.status_code == 422, response.text
    data = response.json()
    assert (data["error"], data["program"]) == ("ssh_interactive_command", "vim")
    assert "Files tab" in data["hint"]
    # Буфер сессии не тронут: команда не уходила
    output = await rig.api.get(f"/api/ssh/sessions/{rig.session_id}/output")
    assert [e["kind"] for e in output.json()["entries"]] == ["opened"]

    forced = await rig.api.post(
        exec_url(rig), json={"command": "vim notes.txt", "allow_interactive": True}
    )
    assert forced.status_code == 200, forced.text


# --------------------------------------------------------------- interrupt


@needs_sh
@needs_timeout_utility
async def test_interrupt_a_running_command(shell_rig: PluginRig):
    rig = shell_rig
    url = f"/api/ssh/sessions/{rig.session_id}/interrupt"
    idle = await rig.api.post(url)
    assert idle.json() == {
        "session_id": rig.session_id,
        "status": "idle",
        "signal": None,
        "command": None,
    }

    running = asyncio.create_task(
        rig.api.post(exec_url(rig), json={"command": "echo started; sleep 20; echo after"})
    )
    await wait_busy(rig)
    stopped = await rig.api.post(url)
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["status"] == "interrupted"
    assert stopped.json()["signal"] == "INT"
    assert stopped.json()["command"] == "echo started; sleep 20; echo after"

    result = (await asyncio.wait_for(running, 10)).json()
    assert result["interrupted"] is True and result["timed_out"] is False
    assert result["exit_code"] == 130
    assert result["stdout"] == "started"
    assert result["duration_ms"] < 10_000
    # Служебное подключение для сигнала закрыто, сессия снова работает
    assert rig.manager.companions(rig.session_id) == []
    after = await rig.api.post(exec_url(rig), json={"command": "echo ok"})
    assert after.json()["stdout"] == "ok" and after.json()["interrupted"] is False


@needs_sh
@needs_timeout_utility
async def test_interrupt_escalates_when_int_is_ignored(shell_rig: PluginRig):
    rig = shell_rig
    running = asyncio.create_task(
        rig.api.post(exec_url(rig), json={"command": "trap '' INT; sleep 20"})
    )
    await wait_busy(rig)
    stopped = await rig.api.post(f"/api/ssh/sessions/{rig.session_id}/interrupt")
    assert (stopped.json()["status"], stopped.json()["signal"]) == ("interrupted", "TERM")
    result = (await asyncio.wait_for(running, 10)).json()
    assert result["interrupted"] is True and result["exit_code"] == 143


@needs_sh
@needs_timeout_utility
async def test_interrupt_from_the_terminal_websocket(shell_rig: PluginRig):
    rig = shell_rig
    async with serve(rig.app) as base:
        async with connect(f"{base}/api/ssh/sessions/{rig.session_id}/ws") as ws:
            assert (await recv_json(ws))["type"] == "hello"
            await send_json(ws, "exec", {"command": "sleep 20", "id": "c1"})
            await wait_busy(rig)
            await send_json(ws, "interrupt")
            seen: dict[str, dict] = {}
            async with asyncio.timeout(10):
                while "interrupt" not in seen or "result" not in seen:
                    message = await recv_json(ws)
                    if message["type"] == "interrupt":
                        seen["interrupt"] = message["data"]
                    elif message["type"] == "output" and message["data"]["kind"] == "result":
                        seen["result"] = message["data"]
            assert seen["interrupt"]["status"] == "interrupted"
            assert seen["result"]["interrupted"] is True
            assert seen["result"]["request_id"] == "c1"

            # Программа, которой нужен терминал: ошибка с подсказкой, команда не уходит
            await send_json(ws, "exec", {"command": "htop", "id": "c2"})
            error = await recv_json(ws)
            assert error["type"] == "error"
            assert (error["data"]["error"], error["data"]["program"]) == (
                "ssh_interactive_command",
                "htop",
            )
            assert error["data"]["request_id"] == "c2"


async def test_interrupt_needs_a_second_connection(rig: PluginRig, monkeypatch):
    session = rig.manager.get(rig.session_id)
    session.running, session.running_marker = "sleep 5", "__SYNEX_0123456789ab__:"
    monkeypatch.setattr(rig.manager, "_channels_supported", False)
    response = await rig.api.post(f"/api/ssh/sessions/{rig.session_id}/interrupt")
    assert (response.status_code, response.json()["error"]) == (409, "ssh_interrupt_unavailable")
    assert session.interrupt_requested is False
    session.running, session.running_marker = None, None


async def test_terminal_commands_get_a_longer_limit(rig: PluginRig, monkeypatch):
    seen: list[float | None] = []
    original = rig.manager.execute

    async def spy(*args, **kwargs):
        seen.append(kwargs.get("timeout"))
        return await original(*args, **kwargs)

    monkeypatch.setattr(rig.manager, "execute", spy)
    monkeypatch.setattr(settings, "TERMINAL_TIMEOUT", 900)
    async with serve(rig.app) as base:
        async with connect(f"{base}/api/ssh/sessions/{rig.session_id}/ws") as ws:
            await recv_json(ws)
            await send_json(ws, "exec", {"command": "echo 1", "id": "a"})
            await send_json(ws, "exec", {"command": "echo 2", "timeout": 5, "id": "b"})
            results = 0
            async with asyncio.timeout(5):
                while results < 2:
                    message = await recv_json(ws)
                    if message["type"] == "output" and message["data"]["kind"] == "result":
                        results += 1
    await rig.api.post(exec_url(rig), json={"command": "echo 3"})
    # Терминал без своего лимита: TERMINAL_TIMEOUT; свой лимит и REST как раньше
    assert seen == [900, 5, None]
