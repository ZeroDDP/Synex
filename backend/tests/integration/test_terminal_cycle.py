"""Терминал полностью: UI → FastAPI → synex_worker → SSH-сервер. Прерывание команды
идёт вторым SSH-подключением, сигнал получают настоящие процессы на сервере."""

from __future__ import annotations

import asyncio

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
    assert (stopped.json()["status"], stopped.json()["signal"]) == ("interrupted", "INT")
    result = (await asyncio.wait_for(running, 15)).json()
    assert result["interrupted"] is True and result["exit_code"] == 130
    assert result["stdout"] == "started"

    refused = await ui.api.post(f"{base}/exec", json={"command": "top"})
    assert (refused.status_code, refused.json()["error"]) == (422, "ssh_interactive_command")

    history = (await ui.api.get(f"{base}/history")).json()
    assert history["commands"][-1] == "echo started; sleep 30; echo after"
