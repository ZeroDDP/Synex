"""Терминал полностью: UI → FastAPI → synex_worker → SSH-сервер. Прерывание команды
идёт вторым SSH-подключением, сигнал получают настоящие процессы на сервере."""

from __future__ import annotations

import asyncio
import shutil

import pytest

from tests.integration.sshd import SSHTarget
from tests.integration.ui import UI


async def test_terminal_on_the_server(ui: UI, ssh_target: SSHTarget):
    session_id = await ui.open_session(ssh_target.connection())
    base = f"/api/ssh/sessions/{session_id}"

    running = asyncio.create_task(
        ui.api.post(f"{base}/exec", json={"command": "echo started; sleep 30; echo after"})
    )
    async with asyncio.timeout(10):
        while not (await ui.api.get(base)).json()["busy"]:
            await asyncio.sleep(0.1)
    await asyncio.sleep(0.5)
    stopped = await ui.api.post(f"{base}/interrupt")
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["status"] == "interrupted"
    signal = stopped.json()["signal"]
    assert signal in ("INT", "TERM")
    result = (await asyncio.wait_for(running, 15)).json()
    assert result["interrupted"] is True
    if signal == "TERM":
        assert result["exit_code"] == 143
    else:
        assert result["exit_code"] == 130
        assert result["stdout"] == "started"
    assert (await ui.exec(session_id, "echo ready"))["stdout"] == "ready"

    refused = await ui.api.post(f"{base}/exec", json={"command": "top"})
    assert (refused.status_code, refused.json()["error"]) == (422, "ssh_interactive_command")

    history = (await ui.api.get(f"{base}/history")).json()
    assert history["commands"][-2] == "echo started; sleep 30; echo after"


@pytest.mark.skipif(not shutil.which("bash"), reason="the local sshd uses this machine's bash")
async def test_completion_on_the_server(ui: UI, ssh_target: SSHTarget):
    session_id = await ui.open_session(ssh_target.connection())
    base = f"/api/ssh/sessions/{session_id}"
    made = await ui.exec(session_id, "mkdir -p synex-it-complete/inner && cd synex-it-complete")
    assert made["exit_code"] == 0, made
    try:
        found = (await ui.api.get(f"{base}/complete", params={"line": "ls in"})).json()
        assert found["candidates"] == ["inner/"]
        commands = (await ui.api.get(f"{base}/complete", params={"line": "ech"})).json()
        assert "echo" in commands["candidates"]
    finally:
        await ui.exec(session_id, "cd && rm -rf synex-it-complete")
