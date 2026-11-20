"""Терминал поверх команд сессии: прерывание идущей команды (Ctrl+C).

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


async def test_interrupt_needs_a_second_connection(rig: PluginRig, monkeypatch):
    session = rig.manager.get(rig.session_id)
    session.running, session.running_marker = "sleep 5", "__SYNEX_0123456789ab__:"
    monkeypatch.setattr(rig.manager, "_channels_supported", False)
    response = await rig.api.post(f"/api/ssh/sessions/{rig.session_id}/interrupt")
    assert (response.status_code, response.json()["error"]) == (409, "ssh_interrupt_unavailable")
    assert session.interrupt_requested is False
    session.running, session.running_marker = None, None
