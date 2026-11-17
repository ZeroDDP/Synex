"""Сценарий MVP целиком (SYN-71): «подключился — передал файл — смотрит метрики —
перезапустил контейнер». UI → FastAPI → synex_worker → SSH-сервер → Docker на сервере.

Тест идёт так же, как пользователь в UI: сохраняет сервер, подключается, отправляет файл
в файловом менеджере, смотрит дашборд и управляет контейнерами, всё через HTTP API и
/ws с заголовком Origin страницы UI. По ходу он проверяет измеримые критерии
успешности из устава и в конце печатает сводку (`pytest -s`):

  * SSH-подключение не дольше 3 с, команда выполняется верно;
  * метрики CPU и RAM приходят не реже 1 раза в секунду, данные в них не старше 500 мс;
  * файл (по умолчанию 100 МБ, SYNEX_TEST_SFTP_MB) уходит на сервер без ошибок и
    совпадает по SHA-256, прогресс виден в /ws;
  * три контейнера: перезапуск, остановка, запуск и логи каждого, живой лог по
    WebSocket показывает перезапуск.

Нужны локальный sshd и работающий Docker; иначе тест пропускается.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import statistics
import time
import uuid
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

from app.core.config import settings
from tests.integration import docker_image
from tests.integration.sshd import SSHTarget
from tests.integration.ui import UI, UI_ORIGIN, Stream

LOCAL_SERVER = not os.environ.get("SYNEX_TEST_SSH_HOST")
FILE_MB = int(os.environ.get("SYNEX_TEST_SFTP_MB", "100"))
MB = 1024 * 1024

pytestmark = pytest.mark.skipif(
    not LOCAL_SERVER or not docker_image.docker_works(),
    reason="needs local sshd and a running Docker",
)


@pytest.fixture(autouse=True)
def all_plugins(monkeypatch):
    monkeypatch.setattr(settings, "PLUGINS_ENABLED", "*")


@pytest.fixture
def containers(tmp_path: Path):
    """Три запущенных контейнера, которые пишут в лог раз в секунду (без сети)."""
    prefix = f"synex-mvp-{uuid.uuid4().hex[:8]}"
    image = f"{prefix}:test"
    names = [f"{prefix}-{letter}" for letter in "abc"]
    docker_image.build_image(image, tmp_path)
    try:
        for name in names:
            docker_image.run_ticker(name, image)
        yield names
    finally:
        docker_image.remove(*names, image=image)


def write_random_file(path: Path, megabytes: int) -> str:
    digest = hashlib.sha256()
    with path.open("wb") as file:
        for _ in range(megabytes):
            chunk = os.urandom(MB)
            digest.update(chunk)
            file.write(chunk)
    return digest.hexdigest()


async def test_mvp_scenario(ui: UI, ssh_target: SSHTarget, containers: list[str], tmp_path: Path):
    summary: list[tuple[str, str]] = []

    async with ui.events() as events:
        # ------------------------------------------------ 1. подключился
        saved = await ui.api.post(
            "/api/connections",
            json={
                "name": f"mvp-{uuid.uuid4().hex[:8]}",
                "host": ssh_target.host,
                "port": ssh_target.port,
                "username": ssh_target.username,
                "auth": "key_path",
                "key_path": ssh_target.key_path,
            },
        )
        assert saved.status_code == 201, saved.text
        connection_id = saved.json()["id"]

        started = time.monotonic()
        opened = await ui.api.post(f"/api/connections/{connection_id}/connect")
        connect_seconds = time.monotonic() - started
        assert opened.status_code == 201, opened.text
        session_id = opened.json()["session_id"]
        await events.wait_for(
            "ssh.session",
            lambda data: data["action"] == "opened" and data["session_id"] == session_id,
        )
        assert connect_seconds <= 3, f"SSH connection took {connect_seconds:.2f}s"
        whoami = await ui.exec(session_id, "whoami && echo $((6 * 7))")
        assert (whoami["stdout"], whoami["exit_code"]) == (f"{ssh_target.username}\n42", 0)
        summary.append(("SSH-подключение ≤ 3 с", f"{connect_seconds:.2f} с, команда верна"))

        # Для файлов и Docker UI открывает свои сессии к тому же серверу: долгая передача
        # и команды контейнеров не ждут терминал (как советует docs/ui-integration.md)
        files_session = (await ui.api.post(f"/api/connections/{connection_id}/connect")).json()
        docker_session = (await ui.api.post(f"/api/connections/{connection_id}/connect")).json()
        files = f"/api/sftp/sessions/{files_session['session_id']}"
        docker = f"/api/docker/sessions/{docker_session['session_id']}"

        # ------------------------------------------------ 2. передал файл
        source = tmp_path / "report.bin"
        expected = write_random_file(source, FILE_MB)
        size = source.stat().st_size
        directory = f"/tmp/synex-mvp-{uuid.uuid4().hex[:12]}"
        made = await ui.api.post(f"{files}/mkdir", json={"path": directory})
        assert made.status_code == 201, made.text
        try:
            started = time.monotonic()
            transfer = await ui.api.post(
                f"{files}/transfers",
                json={"direction": "upload", "local_path": str(source), "remote_path": directory},
            )
            assert transfer.status_code == 202, transfer.text
            transfer_id = transfer.json()["id"]
            progress: list[dict] = []
            async with asyncio.timeout(60 + FILE_MB):
                while not progress or progress[-1]["status"] in ("queued", "running", "verifying"):
                    message = await events.next(timeout=60 + FILE_MB)
                    if message["type"] == "sftp.transfer" and message["data"]["id"] == transfer_id:
                        progress.append(message["data"])
            transfer_seconds = time.monotonic() - started
            final = progress[-1]
            assert final["status"] == "completed", final
            assert (final["bytes_done"], final["checksum"]) == (size, expected)
            assert any(p["status"] == "running" for p in progress)

            listing = (await ui.api.get(f"{files}/list", params={"path": directory})).json()
            assert [(e["name"], e["size"]) for e in listing["entries"]] == [("report.bin", size)]
            remote = await ui.exec(session_id, f"sha256sum {directory}/report.bin")
            assert remote["stdout"].split()[0] == expected
        finally:
            await ui.api.delete(f"{files}/entry", params={"path": directory, "recursive": "true"})
        summary.append(
            (
                f"SFTP: файл {FILE_MB} МБ без ошибок",
                f"{transfer_seconds:.1f} с ({size / MB / transfer_seconds:.1f} МБ/с), "
                f"SHA-256 совпадает, событий прогресса {len(progress)}",
            )
        )

        # ------------------------------------------------ 3. смотрит метрики
        window = 5.0
        samples: list[dict] = []
        started = time.monotonic()
        while time.monotonic() - started < window:
            message = await events.wait_for("metrics", timeout=window)
            server = next(
                (s for s in message["data"]["servers"] if session_id in s["session_ids"]), None
            )
            if server is not None and server["status"] == "ok":
                samples.append(server)
        rate = len(samples) / window
        ages = [s["age_ms"] for s in samples]
        assert rate >= 1, f"metrics at {rate:.1f} Hz"
        assert statistics.median(ages) <= 500, ages
        last = samples[-1]
        assert 0 <= last["cpu"]["percent"] <= 100 * (os.cpu_count() or 1)
        assert 0 < last["memory"]["percent"] < 100 and last["memory"]["total"] > 0
        assert any(d["mount"] == "/" for d in last["disks"] or []), last["disks"]
        assert last["disk"] is not None and 0 <= last["disk"]["percent"] <= 100, last["disk"]
        summary.append(
            (
                "Метрики CPU/RAM ≥ 1 Гц, задержка ≤ 500 мс",
                f"{rate:.1f} Гц, возраст данных: медиана {statistics.median(ages):.0f} мс, "
                f"макс {max(ages)} мс; CPU {last['cpu']['percent']:.1f} %, "
                f"RAM {last['memory']['percent']:.1f} %",
            )
        )

        # ------------------------------------------------ 4. перезапустил контейнер
        listed = {c["name"]: c for c in (await ui.api.get(f"{docker}/containers")).json()}
        assert all(listed[name]["state"] == "running" for name in containers)
        first = containers[0]
        url = f"{ui.ws_base}{docker}/containers/{first}/logs/ws?tail=1"
        async with connect(url, origin=UI_ORIGIN) as ws:
            live = Stream(ws)
            assert (await live.next())["type"] == "hello"
            await live.wait_for("logs", lambda data: bool(data["lines"]))

            started = time.monotonic()
            restarted = await ui.api.post(
                f"{docker}/containers/{first}/restart", params={"timeout": 1}
            )
            restart_seconds = time.monotonic() - started
            assert restarted.status_code == 200, restarted.text
            # Счётчик в контейнере начался заново: живой лог это показал сам
            await live.wait_for(
                "logs", lambda data: any(line["text"] == "tick 1" for line in data["lines"])
            )

        for name in containers[1:]:
            for action, state in (("stop", "exited"), ("start", "running")):
                response = await ui.api.post(
                    f"{docker}/containers/{name}/{action}", params={"timeout": 1}
                )
                assert response.status_code == 200, response.text
                listed = {c["name"]: c for c in (await ui.api.get(f"{docker}/containers")).json()}
                assert listed[name]["state"] == state, (action, listed[name])
        await asyncio.sleep(1.2)
        for name in containers:
            logs = await ui.api.get(f"{docker}/containers/{name}/logs", params={"tail": 20})
            lines = logs.json()["lines"]
            assert any(line["text"].startswith("tick ") for line in lines), (name, lines)
        summary.append(
            (
                "Docker: перезапуск, остановка, запуск и логи трёх контейнеров",
                f"restart {restart_seconds:.1f} с, живой лог показал перезапуск",
            )
        )

        # Терминал пользователя за всё время служебных команд не видел и работает
        output = (await ui.api.get(f"/api/ssh/sessions/{session_id}/output")).json()
        commands = [e["command"] for e in output["entries"] if e["kind"] == "command"]
        assert commands == ["whoami && echo $((6 * 7))", f"sha256sum {directory}/report.bin"]

        # ------------------------------------------------ отключился
        for sid in (session_id, files_session["session_id"], docker_session["session_id"]):
            assert (await ui.api.delete(f"/api/ssh/sessions/{sid}")).status_code == 200
        assert (await ui.api.get("/api/ssh/sessions")).json() == []
        await ui.api.delete(f"/api/connections/{connection_id}")

    width = max(len(title) for title, _ in summary)
    print("\nСценарий MVP: подключился — передал файл — смотрит метрики — перезапустил контейнер")
    for title, result in summary:
        print(f"  {title.ljust(width)}  {result}")
