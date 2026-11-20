"""Логи сервера полностью: UI → FastAPI → synex_worker → SSH-сервер → journalctl, файлы.

Файл лога тест пишет сам, поэтому эта часть идёт только с локальным sshd (сервер — эта же
машина). Журнал systemd есть не везде (macOS, контейнеры): тогда проверяется понятный
ответ «журнала нет».
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

from app.core.config import settings
from tests.integration.sshd import SSHTarget
from tests.integration.ui import UI, UI_ORIGIN, Stream

LOCAL_SERVER = not os.environ.get("SYNEX_TEST_SSH_HOST")


@pytest.fixture(autouse=True)
def fast_logs(monkeypatch):
    monkeypatch.setattr(settings, "PLUGINS_ENABLED", "*")
    monkeypatch.setattr(settings, "LOGS_INTERVAL", 0.2)


def server_of(target: SSHTarget) -> str:
    return f"{target.host.lower()}:{target.port}"


async def test_journal_of_the_selected_server(ui: UI, ssh_target: SSHTarget):
    session_id = await ui.open_session(ssh_target.connection())
    base = f"/api/logs/servers/{server_of(ssh_target)}"

    sources = await ui.api.get(f"{base}/sources")
    assert sources.status_code == 200, sources.text
    assert sources.json()["session_id"] == session_id
    journal = sources.json()["journal"]

    page = await ui.api.get(f"{base}/journal", params={"lines": 5})
    if not journal["available"]:
        assert (page.status_code, page.json()["error"]) == (404, "logs_journal_unavailable")
        return
    assert page.status_code == 200, page.text
    entries = page.json()["entries"]
    assert len(entries) <= 5
    times = [entry["time"] for entry in entries]
    assert times == sorted(times)
    if entries:
        older = await ui.api.get(
            f"{base}/journal", params={"lines": 5, "before": page.json()["before"]}
        )
        assert older.status_code == 200, older.text
        assert all(e["time"] <= times[0] for e in older.json()["entries"])

    bad = await ui.api.get(f"{base}/journal", params={"since": "not a time at all"})
    assert (bad.status_code, bad.json()["error"]) == (422, "logs_bad_filter")

    # Живой журнал отвечает и держит опрос
    url = f"{ui.ws_base}{base}/journal/ws?lines=3"
    async with connect(url, origin=UI_ORIGIN) as ws:
        stream = Stream(ws)
        hello = await stream.next()
        assert hello["type"] == "hello" and hello["data"]["source"] == "journal"
        first = await stream.wait_for("entries")
        assert len(first["data"]["entries"]) <= 3


@pytest.mark.skipif(not LOCAL_SERVER, reason="the test writes the log file on the server")
async def test_live_log_file_through_the_worker(ui: UI, ssh_target: SSHTarget, tmp_path: Path):
    await ui.open_session(ssh_target.connection())
    base = f"/api/logs/servers/{server_of(ssh_target)}"
    folder = tmp_path / "logs"
    folder.mkdir()
    log = folder / "app.log"
    log.write_text("".join(f"start {i}\n" for i in range(5)))

    listed = await ui.api.get(f"{base}/files", params={"dir": str(folder)})
    assert listed.status_code == 200, listed.text
    assert [f["name"] for f in listed.json()["files"]] == ["app.log"]

    tail = await ui.api.get(f"{base}/file", params={"path": str(log), "lines": 2})
    assert tail.status_code == 200, tail.text
    assert [line["text"] for line in tail.json()["lines"]] == ["start 3", "start 4"]

    url = f"{ui.ws_base}{base}/file/ws?path={log}&lines=1"
    async with connect(url, origin=UI_ORIGIN) as ws:
        stream = Stream(ws)
        hello = await stream.next()
        assert hello["data"]["companion"] is True
        first = await stream.wait_for("lines")
        assert [line["text"] for line in first["data"]["lines"]] == ["start 4"]
        with log.open("a") as f:
            f.write("added 1\nadded 2\n")
        later = await stream.wait_for("lines")
        assert [line["text"] for line in later["data"]["lines"]] == ["added 1", "added 2"]

        # Команда в терминале идёт долго, а живой лог не ждёт её: своя служебная сессия
        session_id = hello["data"]["session_id"]
        command = asyncio.create_task(ui.exec(session_id, "sleep 3; echo done"))
        await asyncio.sleep(0.5)
        with log.open("a") as f:
            f.write("while busy\n")
        during = await stream.wait_for("lines", timeout=2.5)
        assert [line["text"] for line in during["data"]["lines"]] == ["while busy"]
        assert not command.done()
        assert (await command)["stdout"] == "done"
