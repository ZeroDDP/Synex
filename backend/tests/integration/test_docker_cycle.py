"""Docker полностью: UI → FastAPI → synex_worker → SSH-сервер → docker на сервере.

Контейнеры настоящие и запущенные (образ собирается из программ этой машины, без сети,
см. docker_image.py). Критерий MVP: логи не менее чем трёх контейнеров. Тесты идут только
с локальным sshd и работающим Docker, иначе пропускаются.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

from app.core.config import settings
from tests.integration import docker_image
from tests.integration.sshd import SSHTarget
from tests.integration.ui import UI, UI_ORIGIN, Stream

LOCAL_SERVER = not os.environ.get("SYNEX_TEST_SSH_HOST")

pytestmark = pytest.mark.skipif(
    not LOCAL_SERVER or not docker_image.docker_works(),
    reason="needs local sshd and a running Docker",
)


@pytest.fixture(autouse=True)
def all_plugins(monkeypatch):
    monkeypatch.setattr(settings, "PLUGINS_ENABLED", "*")


@pytest.fixture(scope="module")
def tickers(tmp_path_factory) -> list[str]:
    """Три запущенных контейнера, которые пишут в лог раз в секунду."""
    prefix = f"synex-it-{uuid.uuid4().hex[:8]}"
    image = f"{prefix}:test"
    names = [f"{prefix}-{letter}" for letter in "abc"]
    docker_image.build_image(image, tmp_path_factory.mktemp("image"))
    try:
        for name in names:
            docker_image.run_ticker(name, image)
        yield names
    finally:
        docker_image.remove(*names, image=image)


async def test_logs_of_three_containers_and_a_live_stream(
    ui: UI, ssh_target: SSHTarget, tickers: list[str]
):
    session_id = await ui.open_session(ssh_target.connection())
    base = f"/api/docker/sessions/{session_id}"

    containers = {c["name"]: c for c in (await ui.api.get(f"{base}/containers")).json()}
    for name in tickers:
        assert containers[name]["state"] == "running", containers[name]
        assert containers[name]["labels"]["synex.test"] == "1"

    # Логи каждого из трёх контейнеров читаются, время строк растёт
    await asyncio.sleep(2)
    for name in tickers:
        logs = (await ui.api.get(f"{base}/containers/{name}/logs", params={"tail": 50})).json()
        texts = [line["text"] for line in logs["lines"]]
        assert texts[0] == "tick 1" and len(texts) >= 2, texts
        times = [line["time"] for line in logs["lines"]]
        assert times == sorted(times) and logs["next_since"] == times[-1]

    # Живой лог: последние строки сразу, новые по мере появления, без повторов
    url = f"{ui.ws_base}{base}/containers/{tickers[0]}/logs/ws?tail=1"
    async with connect(url, origin=UI_ORIGIN) as ws:
        stream = Stream(ws)
        hello = await stream.next()
        assert (hello["type"], hello["data"]["container"]) == ("hello", tickers[0])
        seen: list[dict] = []
        async with asyncio.timeout(30):
            while len([line for line in seen if line["stream"] == "stdout"]) < 4:
                message = await stream.wait_for("logs")
                seen += message["data"]["lines"]
        ticks = [int(line["text"].split()[1]) for line in seen if line["stream"] == "stdout"]
        assert ticks == list(range(ticks[0], ticks[0] + len(ticks))), ticks
        assert len({line["time"] for line in seen}) == len(seen)
        next_since = message["data"]["next_since"]

    # Переподключение с since: строки не повторяются
    async with connect(f"{url}&since={next_since}", origin=UI_ORIGIN) as ws:
        stream = Stream(ws)
        await stream.next()
        fresh = (await stream.wait_for("logs", lambda data: bool(data["lines"])))["data"]
        assert all(line["time"] > next_since for line in fresh["lines"])

    # Терминал сессии служебных команд docker не видел
    output = (await ui.api.get(f"/api/ssh/sessions/{session_id}/output")).json()
    assert [e["kind"] for e in output["entries"]] == ["opened"]


async def test_restart_stop_and_start_a_container(
    ui: UI, ssh_target: SSHTarget, tickers: list[str]
):
    session_id = await ui.open_session(ssh_target.connection())
    base = f"/api/docker/sessions/{session_id}"
    name = tickers[2]

    async def state() -> dict:
        containers = (await ui.api.get(f"{base}/containers")).json()
        return next(c for c in containers if c["name"] == name)

    restarted = await ui.api.post(f"{base}/containers/{name}/restart", params={"timeout": 1})
    assert restarted.status_code == 200, restarted.text
    assert restarted.json() == {"container": name, "action": "restart", "output": name}
    assert (await state())["state"] == "running"
    # После перезапуска счётчик в логе начинается заново
    await asyncio.sleep(1.5)
    logs = (await ui.api.get(f"{base}/containers/{name}/logs", params={"tail": 2})).json()
    assert any(line["text"] in ("tick 1", "tick 2") for line in logs["lines"]), logs

    stopped = await ui.api.post(f"{base}/containers/{name}/stop", params={"timeout": 1})
    assert stopped.status_code == 200, stopped.text
    assert (await state())["state"] == "exited"
    conflict = await ui.api.post(f"{base}/containers/{name}/pause")
    assert (conflict.status_code, conflict.json()["error"]) == (409, "docker_container_conflict")

    started = await ui.api.post(f"{base}/containers/{name}/start")
    assert started.status_code == 200, started.text
    assert (await state())["state"] == "running"

    stats = (await ui.api.get(f"{base}/stats")).json()
    assert {s["name"] for s in stats} >= set(tickers)


def test_image_helper_builds_a_runnable_image(tmp_path: Path):
    """Сам помощник: образ собирается и контейнер из него работает."""
    tag = f"synex-it-{uuid.uuid4().hex[:8]}:probe"
    name = tag.replace(":", "-")
    docker_image.build_image(tag, tmp_path)
    try:
        docker_image.docker("run", "--name", name, "--network", "none", tag,
                            "/bin/sh", "-c", "echo works")  # fmt: skip
        assert docker_image.docker("logs", name).stdout.strip() == "works"
    finally:
        docker_image.remove(name, image=tag)
