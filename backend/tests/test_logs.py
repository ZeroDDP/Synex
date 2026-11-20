"""Плагин logs: журнал systemd и файлы логов сервера, выбранного на Dashboard.

Журнал: фейковый journalctl (tests/fake_journal.py) в эмуляторе shell, тесты идут на
любой ОС. Файлы: настоящие скрипты в настоящем sh (Linux и macOS), файлы во временной
папке теста.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from app.core.config import settings
from app.plugins.logs import files as logfiles
from app.plugins.logs import journal
from app.plugins.logs import service as logs_service
from app.plugins.logs.stream import active_streams
from tests.conftest import needs_timeout_utility
from tests.fake_journal import FakeJournal
from tests.plugin_rig import PluginRig, plugin_rig
from tests.ssh_helpers import recv_json, serve

SERVER = "10.0.0.1:22"
BASE = f"/api/logs/servers/{SERVER}"

needs_sh = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("sh"), reason="needs a real sh"
)


@pytest.fixture(autouse=True)
def fast_logs(monkeypatch):
    monkeypatch.setattr(settings, "LOGS_INTERVAL", 0.05)
    logs_service._no_grep.clear()
    logs_service._no_output_fields.clear()


def texts(message: dict) -> list[str]:
    """Тексты строк файла из ответа или сообщения живого лога."""
    return [line["text"] for line in message["lines"]]


def journal_with_entries() -> FakeJournal:
    fake = FakeJournal()
    fake.add("nginx started", unit="nginx.service")
    fake.add("Accepted publickey for root", unit="ssh.service", identifier="sshd")
    fake.add("upstream timed out", unit="nginx.service", priority=3)
    fake.add("cron job done", unit="cron.service")
    fake.add("Disk almost FULL", unit="nginx.service", priority=4)
    return fake


@pytest.fixture
async def rig(tmp_path: Path, missing_socket_path) -> AsyncIterator[tuple[PluginRig, FakeJournal]]:
    fake = journal_with_entries()
    async with plugin_rig(tmp_path, missing_socket_path, ["logs"], {"journalctl": fake}) as r:
        yield r, fake
    assert active_streams() == 0


# ------------------------------------------------------------------ parsing


def test_journal_entries_are_parsed_from_json_lines():
    lines = [
        json.dumps(
            {
                "__CURSOR": "c1",
                "__REALTIME_TIMESTAMP": "1795000000123456",
                "PRIORITY": "3",
                "_SYSTEMD_UNIT": "nginx.service",
                "SYSLOG_IDENTIFIER": "nginx",
                "_PID": "42",
                "MESSAGE": "boom",
            }
        ),
        # Сообщение не в UTF-8 приходит списком байтов, длинное без --all — null
        json.dumps(
            {
                "__CURSOR": "c2",
                "__REALTIME_TIMESTAMP": "1795000001000000",
                "MESSAGE": [0xD0, 0x9F, 0xFF],
                "_COMM": "app",
            }
        ),  # fmt: skip
        json.dumps(
            {
                "__CURSOR": "c3",
                "__REALTIME_TIMESTAMP": "1795000002000000",
                "MESSAGE": None,
                "PRIORITY": "9",
                "UNIT": "foo.service",
            }
        ),  # fmt: skip
        json.dumps({"MESSAGE": "no cursor"}),
        "-- No entries --",
        "{broken",
    ]
    entries = journal.parse_entries("\n".join(lines))
    assert [e.cursor for e in entries] == ["c1", "c2", "c3"]
    first, second, third = entries
    assert first.as_dict() == {
        "cursor": "c1",
        "time": 1795000000123,
        "priority": 3,
        "level": "err",
        "severity": "error",
        "unit": "nginx.service",
        "identifier": "nginx",
        "pid": 42,
        "hostname": None,
        "boot_id": None,
        "message": "boom",
    }
    assert second.message == "П�" and second.identifier == "app"
    assert third.message == "" and third.priority is None and third.unit == "foo.service"
    assert [e.cursor for e in journal.parse_entries("\n".join(lines), newest_first=True)] == [
        "c3",
        "c2",
        "c1",
    ]


def test_journal_command_arguments():
    query = journal.JournalQuery(
        units=("nginx.service",), priority=3, grep="Error (x)", since="1795000000123"
    )
    command = journal.page_command(query, 50, before="s=1;i=2")
    assert "-u nginx.service -p 3" in command
    assert "-S @1795000000.123" in command
    assert r"-g 'error\ \(x\)'" in command
    assert command.endswith("-r -n 50 '--after-cursor=s=1;i=2'")
    # Вперёд от курсора с поиском -n не работает в journalctl: без него
    assert " -n " not in journal.follow_command(query, "s=1", 100)
    assert journal.follow_command(journal.JournalQuery(), "s=1", 100).endswith("-n 100")
    # Не латиницу journalctl сравнивает с учётом регистра: оставляем как ввели
    assert journal.grep_pattern("Ошибка DB") == "Ошибка\\ db"


def test_journal_probe_reports_access():
    version = "systemd 255 (255.4-1ubuntu8)\n+PAM\n"
    assert journal.parse_probe(version, "", 0).access == "all"
    user = journal.parse_probe(version, "Hint: You are currently not seeing messages from "
                               "other users and the system.", 0)  # fmt: skip
    assert (user.available, user.access, user.version) == (True, "user", "255")
    denied = journal.parse_probe(version, "No journal files were opened due to "
                                 "insufficient permissions.", 0)  # fmt: skip
    assert denied.access == "none"
    empty = journal.parse_probe(version, "No journal files were found.\n", 0)
    assert empty.access == "all" and "empty" in (empty.detail or "")
    missing = journal.parse_probe("", "sh: 1: journalctl: not found", 127)
    assert (missing.available, missing.access) == (False, "none")


def test_file_chunks_keep_lines_whole():
    header = logfiles.HEADER
    # Окно последних байт начинается с середины строки: она отбрасывается
    chunk = logfiles.parse_chunk(f"{header} ok 100 7 90 same 90\nne\nlast\n", hold_partial=False)
    assert (chunk.lines, chunk.offset) == (["last"], 100)
    # Живой лог: недописанная строка ждёт следующего опроса
    chunk = logfiles.parse_chunk(f"{header} ok 30 7 20 same 0\nfull\r\nhalf", hold_partial=True,
                                 limit=1000)  # fmt: skip
    assert (chunk.lines, chunk.offset) == (["full"], 26)
    chunk = logfiles.parse_chunk(f"{header} ok 30 7 26 same 0\nhalf", hold_partial=True,
                                 limit=1000)  # fmt: skip
    assert (chunk.lines, chunk.offset) == ([], 26)
    # Строка длиннее куска: отдаём как есть, иначе застряли бы
    chunk = logfiles.parse_chunk(f"{header} ok 30 7 26 same 0\nhalf", hold_partial=True, limit=4)
    assert (chunk.lines, chunk.offset) == (["half"], 30)
    # Разовое чтение: последняя строка без перевода строки тоже строка
    chunk = logfiles.parse_chunk(f"{header} ok 9 7 0 same 0\nab\ncd", hold_partial=False)
    assert chunk.lines == ["ab", "cd"]
    # Вывод команды приходит без последнего перевода строки: конец байт файла отмечен
    end = f"\n{logfiles.END}"
    chunk = logfiles.parse_chunk(f"{header} ok 9 7 0 same 0\nab\n{end}", hold_partial=True)
    assert (chunk.lines, chunk.offset) == (["ab"], 9)
    chunk = logfiles.parse_chunk(f"{header} ok 9 7 0 same 0\nab\ncd{end}", hold_partial=True)
    assert (chunk.lines, chunk.offset) == (["ab"], 7)
    assert logfiles.parse_chunk(f"{header} denied\n", hold_partial=False).status == "denied"
    assert logfiles.parse_chunk("sh: syntax error", hold_partial=False).status == "missing"


def test_rotated_file_names():
    def rotated(path: str) -> bool:
        return logfiles.LogFile(path, True, 1, None).rotated

    assert rotated("/var/log/syslog.1")
    assert rotated("/var/log/messages-20261120")
    assert not rotated("/var/log/syslog")
    assert not rotated("/var/log/nginx/access.log")


# --------------------------------------------------------------- journal API


async def test_sources_show_the_journal_and_survive_missing_files(rig):
    r, fake = rig
    fake.access = "user"
    response = await r.api.get(f"{BASE}/sources")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["session_id"] == r.session_id
    assert data["journal"]["available"] is True
    assert data["journal"]["access"] == "user"
    assert "adm" in data["journal"]["detail"]
    assert data["directory"] == "/var/log"
    # Эмулятор shell не выполняет скрипт списка файлов: файлов нет, причина в files_detail
    assert data["files"] == [] and data["files_detail"]


async def test_last_entries_and_older_pages(rig):
    r, fake = rig
    response = await r.api.get(f"{BASE}/journal", params={"lines": 2})
    assert response.status_code == 200, response.text
    page = response.json()
    assert [e["message"] for e in page["entries"]] == ["cron job done", "Disk almost FULL"]
    assert page["more"] is True
    assert page["cursor"] == fake.entries[-1]["__CURSOR"]
    entry = page["entries"][1]
    assert (entry["level"], entry["unit"], entry["identifier"]) == ("warning", "nginx.service",
                                                                    "nginx")  # fmt: skip
    assert entry["time"] == int(fake.entries[-1]["__REALTIME_TIMESTAMP"]) // 1000

    older = await r.api.get(f"{BASE}/journal", params={"lines": 2, "before": page["before"]})
    assert [e["message"] for e in older.json()["entries"]] == [
        "Accepted publickey for root",
        "upstream timed out",
    ]
    oldest = await r.api.get(
        f"{BASE}/journal", params={"lines": 2, "before": older.json()["before"]}
    )
    assert [e["message"] for e in oldest.json()["entries"]] == ["nginx started"]
    assert oldest.json()["more"] is False
    # Ответ сокращён: только нужные поля
    assert any(arg.startswith("--output-fields=") for arg in fake.calls[-1])


async def test_journal_filters(rig):
    r, _ = rig

    async def messages(**params) -> list[str]:
        response = await r.api.get(f"{BASE}/journal", params=params)
        assert response.status_code == 200, response.text
        return [e["message"] for e in response.json()["entries"]]

    assert await messages(unit="nginx.service", priority="warning") == [
        "upstream timed out",
        "Disk almost FULL",
    ]
    assert await messages(unit=["cron.service", "ssh*"]) == [
        "Accepted publickey for root",
        "cron job done",
    ]
    # Поиск текста, без учёта регистра латиницы; спецсимволы не регулярка
    assert await messages(grep="full") == ["Disk almost FULL"]
    assert await messages(grep="(") == []
    since = int(journal_with_entries().entries[3]["__REALTIME_TIMESTAMP"]) // 1000
    assert await messages(since=str(since)) == ["cron job done", "Disk almost FULL"]


async def test_bad_filters_are_422(rig):
    r, _ = rig
    for params in ({"since": "next tuesday"}, {"unit": "-x"}, {"before": "s=unknown"}):
        response = await r.api.get(f"{BASE}/journal", params=params)
        assert response.status_code == 422, (params, response.text)
    response = await r.api.get(f"{BASE}/journal", params={"since": "next tuesday"})
    assert response.json()["error"] == "logs_bad_filter"
    assert "Failed to parse timestamp" in response.json()["detail"]
    response = await r.api.get(f"{BASE}/journal", params={"before": "s=unknown"})
    assert response.json()["error"] == "logs_bad_filter"


async def test_server_without_session_and_without_journal(rig):
    r, fake = rig
    response = await r.api.get("/api/logs/servers/10.9.9.9:22/journal")
    assert (response.status_code, response.json()["error"]) == (404, "logs_server_not_connected")
    response = await r.api.get(f"{BASE}/journal", params={"session_id": "nope"})
    assert (response.status_code, response.json()["error"]) == (404, "logs_server_not_connected")
    pinned = await r.api.get(f"{BASE}/journal", params={"session_id": r.session_id, "lines": 1})
    assert pinned.json()["session_id"] == r.session_id

    fake.installed = False
    response = await r.api.get(f"{BASE}/journal")
    assert (response.status_code, response.json()["error"]) == (404, "logs_journal_unavailable")
    sources = await r.api.get(f"{BASE}/sources")
    assert sources.json()["journal"]["available"] is False


async def test_old_journalctl_without_output_fields_and_grep(rig):
    r, fake = rig
    fake.old = True
    response = await r.api.get(f"{BASE}/journal", params={"grep": "nginx", "lines": 5})
    assert response.status_code == 200, response.text
    assert [e["message"] for e in response.json()["entries"]] == ["nginx started"]
    # Повтор без --output-fields, потом без -g (ищем сами); дальше сразу без них
    calls = len(fake.calls)
    await r.api.get(f"{BASE}/journal", params={"grep": "nginx"})
    assert len(fake.calls) == calls + 1
    assert not any(a.startswith("--output-fields") or a == "-g" for a in fake.calls[-1])


async def test_units_for_the_filter(rig):
    r, _ = rig
    response = await r.api.get(f"{BASE}/journal/units")
    assert response.status_code == 200, response.text
    assert response.json()["units"] == ["cron.service", "nginx.service", "ssh.service"]


# ---------------------------------------------------------------- journal ws


async def next_of(ws: ClientConnection, message_type: str, timeout: float = 5.0) -> dict:
    async with asyncio.timeout(timeout):
        while True:
            message = json.loads(await ws.recv())
            if message["type"] == message_type:
                return message["data"]


async def test_live_journal(rig):
    r, fake = rig
    async with serve(r.app) as base:
        url = f"{base}{BASE}/journal/ws?lines=2&unit=nginx.service"
        async with connect(url) as ws:
            hello = await recv_json(ws)
            assert hello["type"] == "hello"
            assert hello["data"]["server"] == SERVER
            assert hello["data"]["source"] == "journal"
            assert hello["data"]["companion"] is True  # опрос в своей служебной сессии
            first = await next_of(ws, "entries")
            assert [e["message"] for e in first["entries"]] == [
                "upstream timed out",
                "Disk almost FULL",
            ]
            fake.add("other unit", unit="cron.service")
            fake.add("reload done", unit="nginx.service")
            later = await next_of(ws, "entries")
            assert [e["message"] for e in later["entries"]] == ["reload done"]
            cursor = later["cursor"]

        # Переподключение с курсором: без повторов, только новое
        fake.add("after reconnect", unit="nginx.service")
        async with connect(f"{base}{BASE}/journal/ws?unit=nginx.service&after={cursor}") as ws:
            await recv_json(ws)
            resumed = await next_of(ws, "entries")
            assert [e["message"] for e in resumed["entries"]] == ["after reconnect"]

        # lines=0: старое не нужно, только новые записи
        async with connect(f"{base}{BASE}/journal/ws?lines=0") as ws:
            await recv_json(ws)
            assert (await next_of(ws, "entries"))["entries"] == []
            fake.add("fresh")
            assert [e["message"] for e in (await next_of(ws, "entries"))["entries"]] == ["fresh"]

            # Закрыли сессию: поток сообщает и закрывается
            await r.api.delete(f"/api/ssh/sessions/{r.session_id}")
            closed = await next_of(ws, "closed")
            assert closed == {"reason": "server_disconnected"}
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 5)
    assert r.manager.companions(r.session_id) == []


async def test_live_journal_with_bad_filter_closes(rig):
    r, _ = rig
    async with serve(r.app) as base:
        async with connect(f"{base}{BASE}/journal/ws?since=garbage") as ws:
            await recv_json(ws)
            error = await next_of(ws, "error")
            assert (error["error"], error["fatal"]) == ("logs_bad_filter", True)
            with pytest.raises(ConnectionClosed) as closed:
                await asyncio.wait_for(ws.recv(), 5)
            assert closed.value.rcvd is not None and closed.value.rcvd.code == 4422

        # Сервера нет: ошибка и закрытие сразу после подключения
        async with connect(f"{base}/api/logs/servers/10.9.9.9:22/journal/ws") as ws:
            error = await next_of(ws, "error")
            assert error["error"] == "logs_server_not_connected"


async def test_live_journal_without_companion_uses_the_session(rig, monkeypatch):
    r, fake = rig
    monkeypatch.setattr(settings, "LOGS_COMPANION", False)
    async with serve(r.app) as base:
        async with connect(f"{base}{BASE}/journal/ws?lines=1") as ws:
            hello = await recv_json(ws)
            assert hello["data"]["companion"] is False
            assert len((await next_of(ws, "entries"))["entries"]) == 1
            fake.add("via main session")
            later = await next_of(ws, "entries")
            assert later["entries"][0]["message"] == "via main session"


# -------------------------------------------------------------------- files


@pytest.fixture
async def shell_rig(tmp_path: Path, missing_socket_path) -> AsyncIterator[PluginRig]:
    async with plugin_rig(tmp_path, missing_socket_path, ["logs"]) as r:
        yield r
    assert active_streams() == 0


@needs_sh
@needs_timeout_utility
async def test_file_tail_grep_and_errors(shell_rig: PluginRig, tmp_path: Path):
    log = tmp_path / "app.log"
    log.write_text("".join(f"line {i}\n" for i in range(1, 11)) + "Warning: disk\nlast")
    response = await shell_rig.api.get(f"{BASE}/file", params={"path": str(log), "lines": 3})
    assert response.status_code == 200, response.text
    data = response.json()
    assert texts(data) == ["line 10", "Warning: disk", "last"]
    assert data["size"] == data["offset"] == log.stat().st_size
    assert data["inode"] == str(log.stat().st_ino)
    assert data["truncated"] is False

    found = await shell_rig.api.get(f"{BASE}/file", params={"path": str(log), "grep": "WARN"})
    assert texts(found.json()) == ["Warning: disk"]

    missing = await shell_rig.api.get(f"{BASE}/file", params={"path": str(tmp_path / "nope")})
    assert (missing.status_code, missing.json()["error"]) == (404, "log_file_not_found")
    folder = await shell_rig.api.get(f"{BASE}/file", params={"path": str(tmp_path)})
    assert (folder.status_code, folder.json()["error"]) == (422, "log_file_is_directory")
    relative = await shell_rig.api.get(f"{BASE}/file", params={"path": "app.log"})
    assert relative.status_code == 422


@needs_sh
@needs_timeout_utility
async def test_file_tail_window(shell_rig: PluginRig, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "LOGS_CHUNK_BYTES", 4096)
    log = tmp_path / "big.log"
    log.write_text("".join(f"{i:05d} " + "x" * 94 + "\n" for i in range(100)))  # 100 байт
    response = await shell_rig.api.get(f"{BASE}/file", params={"path": str(log), "lines": 100})
    data = response.json()
    # В окно 4096 байт помещается 40 строк, первая из них неполная и отброшена
    assert len(data["lines"]) == 40 and texts(data)[-1].startswith("00099")
    assert data["truncated"] is True


@needs_sh
@needs_timeout_utility
async def test_file_list(shell_rig: PluginRig, tmp_path: Path):
    logs = tmp_path / "logs"
    (logs / "nginx").mkdir(parents=True)
    (logs / "syslog").write_text("a\n")
    (logs / "syslog.1").write_text("b\n")
    (logs / "syslog.2.gz").write_bytes(b"\x1f\x8b")
    (logs / "nginx" / "access.log").write_text("GET /\n")
    (logs / "wtmp").write_bytes(b"\0")
    response = await shell_rig.api.get(f"{BASE}/files", params={"dir": str(logs)})
    assert response.status_code == 200, response.text
    files = {f["name"]: f for f in response.json()["files"]}
    assert set(files) == {"syslog", "syslog.1", "access.log"}
    assert files["syslog"]["size"] == 2 and files["syslog"]["readable"] is True
    assert files["syslog"]["modified"] == int((logs / "syslog").stat().st_mtime) * 1000
    assert files["syslog.1"]["rotated"] is True
    assert files["access.log"]["path"] == f"{logs}/nginx/access.log"

    missing = await shell_rig.api.get(f"{BASE}/files", params={"dir": str(tmp_path / "x")})
    assert missing.status_code == 404


@needs_sh
@needs_timeout_utility
async def test_live_file(shell_rig: PluginRig, tmp_path: Path):
    log = tmp_path / "app.log"
    log.write_text("old 1\nold 2\n")
    async with serve(shell_rig.app) as base:
        url = f"{base}{BASE}/file/ws?path={log}&lines=1"
        async with connect(url) as ws:
            hello = await recv_json(ws)
            assert hello["data"]["source"] == "file"
            first = await next_of(ws, "lines")
            assert texts(first) == ["old 2"]
            assert first["rotated"] is False

            with log.open("a") as f:
                f.write("new 1\nnew 2 hal")
            later = await next_of(ws, "lines")
            assert texts(later) == ["new 1"]  # недописанная строка ждёт
            with log.open("a") as f:
                f.write("f done\n")
            assert texts(await next_of(ws, "lines")) == ["new 2 half done"]

            # logrotate: старый файл переименован, новый создан заново
            log.rename(tmp_path / "app.log.1")
            await asyncio.sleep(0.2)
            log.write_text("after rotate\n")
            rotated = await next_of(ws, "lines")
            while not rotated["rotated"]:
                rotated = await next_of(ws, "lines")
            assert texts(rotated) == ["after rotate"]
            offset, inode = rotated["offset"], rotated["inode"]

        # Переподключение с offset и inode: только то, что дописали после
        with log.open("a") as f:
            f.write("while away\n")
        async with connect(f"{url}&offset={offset}&inode={inode}") as ws:
            await recv_json(ws)
            assert texts(await next_of(ws, "lines")) == ["while away"]


@needs_sh
@needs_timeout_utility
async def test_live_file_grep_and_missing_file(shell_rig: PluginRig, tmp_path: Path):
    log = tmp_path / "app.log"
    log.write_text("INFO a\nERROR b\n")
    async with serve(shell_rig.app) as base:
        async with connect(f"{base}{BASE}/file/ws?path={log}&grep=error") as ws:
            await recv_json(ws)
            assert texts(await next_of(ws, "lines")) == ["ERROR b"]
            with log.open("a") as f:
                f.write("INFO c\nerror d\n")
            assert texts(await next_of(ws, "lines")) == ["error d"]
            # Файл пропал после первого чтения: временная помеха, поток живёт
            os.remove(log)
            error = await next_of(ws, "error")
            assert (error["error"], error["fatal"]) == ("log_file_not_found", False)
            log.write_text("error again\n")
            assert texts(await next_of(ws, "lines")) == ["error again"]

        # Файла нет с самого начала: ошибка и закрытие
        async with connect(f"{base}{BASE}/file/ws?path={tmp_path}/none.log") as ws:
            await recv_json(ws)
            error = await next_of(ws, "error")
            assert (error["error"], error["fatal"]) == ("log_file_not_found", True)
            with pytest.raises(ConnectionClosed) as closed:
                await asyncio.wait_for(ws.recv(), 5)
            assert closed.value.rcvd is not None and closed.value.rcvd.code == 4404


# ---------------------------------------------------------------- severity


@pytest.mark.parametrize(
    ("line", "severity"),
    [
        ("2026/11/20 10:00:01 [error] 812#812: upstream timed out", "error"),
        ("Nov 20 10:00:01 web CRON[1]: (root) CMD (run-parts)", "info"),
        ("WARN  disk almost full", "warn"),
        ("level=warning msg=slow", "warn"),
        ("DEBUG connecting", "debug"),
        ("INFO: retry after error", "info"),  # первое слово уровня важнее
        ("FATAL out of memory", "error"),
        ("ordinary line", "info"),
    ],
)
def test_severity_of_file_lines(line: str, severity: str):
    assert logfiles.detect_severity(line) == severity


async def test_journal_severity_tabs(rig):
    r, _ = rig

    async def messages(**params) -> list[str]:
        response = await r.api.get(f"{BASE}/journal", params=params)
        assert response.status_code == 200, response.text
        return [e["message"] for e in response.json()["entries"]]

    # Вкладки UI: ERROR = emerg..err, WARN = только warning, INFO = notice и info
    assert await messages(severity="error") == ["upstream timed out"]
    assert await messages(severity="warn") == ["Disk almost FULL"]
    assert len(await messages(severity="info")) == 3
    entries = (await r.api.get(f"{BASE}/journal")).json()["entries"]
    assert [e["severity"] for e in entries] == ["info", "info", "error", "info", "warn"]
    both = await r.api.get(f"{BASE}/journal", params={"severity": "warn", "priority": "err"})
    assert (both.status_code, both.json()["error"]) == (422, "logs_bad_filter")


@needs_sh
@needs_timeout_utility
async def test_file_severity_filter(shell_rig: PluginRig, tmp_path: Path):
    log = tmp_path / "app.log"
    log.write_text("INFO start\nERROR boom\nWARN slow\nERROR again\n")
    response = await shell_rig.api.get(
        f"{BASE}/file", params={"path": str(log), "severity": "error"}
    )
    assert response.json()["lines"] == [
        {"text": "ERROR boom", "severity": "error"},
        {"text": "ERROR again", "severity": "error"},
    ]
    async with serve(shell_rig.app) as base:
        url = f"{base}{BASE}/file/ws?path={log}&severity=warn"
        async with connect(url) as ws:
            await recv_json(ws)
            assert texts(await next_of(ws, "lines")) == ["WARN slow"]
            with log.open("a") as f:
                f.write("INFO x\nWARNING y\n")
            assert texts(await next_of(ws, "lines")) == ["WARNING y"]
