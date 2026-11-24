"""Плагин docker: разбор вывода docker CLI и API через SSH-сессию (фейковый docker)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.docker_format import (
    PS_FORMAT,
    LogLine,
    Port,
    parse_containers,
    parse_created_at,
    parse_images,
    parse_labels,
    parse_logs,
    parse_ports,
    parse_size,
    parse_stats,
    parse_version,
)
from tests.fake_docker import FakeContainer, FakeDocker, FakeImage
from tests.plugin_rig import plugin_rig

# Вывод настоящего Docker 29.9.1 (docker ps -a --no-trunc --format PS_FORMAT)
PS_OUTPUT = (
    '{"id":"6de63364bd705a5aec82c1a9fbeddbdac5cb57a0b733710227536d6f8e16a2dc","names":"web",'
    '"image":"mirror.gcr.io/library/alpine:3.20",'
    '"command":"\\"sh -c \'echo \\\\\\"out\\\\\\"\'\\"",'
    '"created_at":"2026-11-06 22:42:33 +0700 +07","state":"running","status":"Up 17 minutes",'
    '"ports":"0.0.0.0:18080-\\u003e80/tcp","labels":"com.docker.compose.project=shop,'
    'com.docker.compose.service=web","networks":"bridge"}\n'
    '{"id":"7c788158be9c039ee5659c64c6a0701ceab6a2426b0230a18b0109c112c4f5e8","names":"job",'
    '"image":"alpine","command":"\\"true\\"","created_at":"2026-11-06 22:42:34 +0700 +07",'
    '"state":"exited","status":"Exited (3) 17 minutes ago","ports":"","labels":"",'
    '"networks":"bridge"}\n'
    '{"id":"a1","names":"hc","image":"alpine","command":"\\"sleep 1000\\"","created_at":"",'
    '"state":"running","status":"Up 3 seconds (healthy)","ports":"","labels":"",'
    '"networks":"bridge,backend"}\n'
)
VERSION_OUTPUT = (
    '{"Client":{"Version":"29.9.1","ApiVersion":"1.56","Os":"linux","Arch":"amd64"},'
    '"Server":{"Version":"29.9.1","ApiVersion":"1.56","Os":"linux","Arch":"amd64"}}'
)


def test_parse_containers():
    web, job, hc = parse_containers(PS_OUTPUT + "not json\n")
    assert (web.name, web.state, web.status) == ("web", "running", "Up 17 minutes")
    assert web.exit_code is None
    assert web.command == "sh -c 'echo \\\"out\\\"'"
    assert web.created_at == datetime(2026, 11, 6, 22, 42, 33, tzinfo=timezone(timedelta(hours=7)))
    assert web.ports == [Port("80", "tcp", "0.0.0.0", "18080")]
    assert (web.compose_project, web.compose_service) == ("shop", "web")
    assert (job.state, job.exit_code, job.command) == ("exited", 3, "true")
    assert (hc.health, hc.networks, hc.created_at) == ("healthy", ["bridge", "backend"], None)


def test_parse_ports_labels_and_dates():
    assert parse_ports(
        "0.0.0.0:9000-9001->9000-9001/tcp, 127.0.0.1:18081->81/tcp, [::]:8080->80/tcp, 53/udp"
    ) == [
        Port("9000-9001", "tcp", "0.0.0.0", "9000-9001"),
        Port("81", "tcp", "127.0.0.1", "18081"),
        Port("80", "tcp", "::", "8080"),
        Port("53", "udp"),
    ]
    assert parse_ports("") == []
    assert parse_labels("a=1,traefik.rule=Host(`x`) || Host(`y`),b=") == {
        "a": "1",
        "traefik.rule": "Host(`x`) || Host(`y`)",
        "b": "",
    }
    assert parse_labels("hosts=a,b,c=d") == {"hosts": "a,b", "c": "d"}
    assert parse_created_at("2026-11-06 15:42:33 +0000 UTC") is not None
    assert parse_created_at("garbage") is None


def test_parse_version():
    version = parse_version(VERSION_OUTPUT)
    assert (version.client_version, version.server_version, version.api_version) == (
        "29.9.1",
        "29.9.1",
        "1.56",
    )
    without_server = parse_version('{"Client":{"Version":"29.9.1"},"Server":null}')
    assert (without_server.client_version, without_server.server_version) == ("29.9.1", None)
    assert parse_version("").client_version is None


def test_parse_logs_merges_streams_by_time():
    # docker logs --timestamps: строки stdout контейнера в stdout docker, stderr в stderr
    stdout = (
        "2026-11-06T15:42:48.037528969Z out 14\n"
        "2026-11-06T15:42:49.038635274Z progress 50%\r\n"
        "2026-11-06T15:42:50.000000000Z multi\nline\n"
    )
    stderr = "2026-11-06T15:42:48.037615554Z err 14\n2026-11-06T15:42:49.038604951Z err 15\n"
    lines = parse_logs(stdout, stderr)
    assert lines == [
        LogLine("2026-11-06T15:42:48.037528969Z", "stdout", "out 14"),
        LogLine("2026-11-06T15:42:48.037615554Z", "stderr", "err 14"),
        LogLine("2026-11-06T15:42:49.038604951Z", "stderr", "err 15"),
        LogLine("2026-11-06T15:42:49.038635274Z", "stdout", "progress 50%"),
        LogLine("2026-11-06T15:42:50.000000000Z", "stdout", "multi\nline"),
    ]
    # since: строка с этим временем уже показана
    later = parse_logs(stdout, stderr, since="2026-11-06T15:42:49.038604951Z")
    assert [line.text for line in later] == ["progress 50%", "multi\nline"]
    assert len(parse_logs(stdout, stderr, since="10m")) == 5  # длительность: без фильтра


def test_parse_stats_and_sizes():
    # Вывод Docker 29.9.1: docker stats --no-stream --no-trunc --format '{{json .}}'
    (stats,) = parse_stats(
        '{"BlockIO":"4.1MB / 0B","CPUPerc":"150.25%","Container":"6de6","ID":"6de6",'
        '"MemPerc":"0.01%","MemUsage":"1.07MiB / 7.834GiB","Name":"web",'
        '"NetIO":"1.2kB / 126B","PIDs":"2"}\n'
    )
    assert (stats.name, stats.cpu_percent, stats.memory_percent, stats.pids) == (
        "web",
        150.25,
        0.01,
        2,
    )
    assert (stats.memory_used, stats.memory_limit) == (round(1.07 * 2**20), round(7.834 * 2**30))
    assert (stats.net_rx, stats.net_tx, stats.block_read, stats.block_write) == (
        1200,
        126,
        4_100_000,
        0,
    )
    assert [parse_size(v) for v in ("0B", "512KiB", "1.5GB", "--", "", "12 parsecs")] == [
        0,
        512 * 1024,
        1_500_000_000,
        None,
        None,
        None,
    ]


def test_parse_images():
    first, unnamed = parse_images(
        '{"id":"sha256:d9e8","repository":"nginx","tag":"1.27","digest":"\\u003cnone\\u003e",'
        '"created_at":"2026-04-17 06:53:26 +0700 +07","size":"12.2MB"}\n'
        '{"id":"sha256:aaaa","repository":"\\u003cnone\\u003e","tag":"\\u003cnone\\u003e",'
        '"digest":"sha256:ffff","created_at":"","size":"7.8MB"}\n'
    )
    assert (first.repository, first.tag, first.digest, first.size) == (
        "nginx",
        "1.27",
        None,
        12_200_000,
    )
    assert (unnamed.repository, unnamed.tag, unnamed.digest) == (None, None, "sha256:ffff")


def test_ps_format_has_no_size():
    # .Size заставил бы docker считать размер каждого контейнера (медленно)
    assert ".Size" not in PS_FORMAT


# --------------------------------------------------------------------- API


WEB_LOGS = [
    ("2026-11-06T15:42:48.000000001Z", "stdout", "GET / 200"),
    ("2026-11-06T15:42:48.000000002Z", "stderr", "warning: slow"),
    ("2026-11-06T15:42:49.000000003Z", "stdout", "GET /api 500"),
]


def containers() -> list[FakeContainer]:
    return [
        FakeContainer(
            id="6de63364bd705a5aec82c1a9fbeddbdac5cb57a0b733710227536d6f8e16a2dc",
            name="web",
            ports="0.0.0.0:8080->80/tcp",
            labels="com.docker.compose.project=shop,com.docker.compose.service=web",
            logs=list(WEB_LOGS),
        ),
        FakeContainer(
            id="7c788158be9c039ee5659c64c6a0701ceab6a2426b0230a18b0109c112c4f5e8",
            name="job",
            image="alpine",
            command='"true"',
            state="exited",
            status="Exited (3) 2 hours ago",
        ),
    ]


@pytest.fixture
def docker() -> FakeDocker:
    return FakeDocker(
        containers=containers(),
        images=[
            FakeImage("sha256:d9e8", "nginx", "1.27"),
            FakeImage("sha256:aaaa", "<none>", "<none>"),
        ],
    )


@pytest.fixture
async def rig(tmp_path: Path, missing_socket_path, docker):
    async with plugin_rig(tmp_path, missing_socket_path, ["docker"], {"docker": docker}) as rig:
        yield rig


async def test_info(rig, docker):
    url = f"/api/docker/{rig.session_url}/info"
    info = (await rig.api.get(url)).json()
    assert info == {
        "status": "ok",
        "detail": None,
        "client_version": None,
        "server_version": "29.9.1",
        "api_version": "1.56",
        "os": "linux",
        "arch": "amd64",
    }

    docker.daemon = False
    down = (await rig.api.get(url)).json()
    assert (down["status"], down["client_version"], down["server_version"]) == (
        "daemon_unavailable",
        None,
        None,
    )
    assert "failed to connect to the docker API" in down["detail"]

    docker.permission = False
    assert (await rig.api.get(url)).json()["status"] == "permission_denied"

    rig.worker.shell.programs.pop("docker")  # docker не установлен
    missing = (await rig.api.get(url)).json()
    assert (missing["status"], missing["client_version"]) == ("not_installed", None)


async def test_containers(rig, docker):
    response = await rig.api.get(f"/api/docker/{rig.session_url}/containers")
    assert response.status_code == 200, response.text
    web, job = response.json()
    assert (web["name"], web["state"], web["compose_project"]) == ("web", "running", "shop")
    assert web["ports"] == [
        {"container_port": "80", "protocol": "tcp", "host_ip": "0.0.0.0", "host_port": "8080"}
    ]
    assert (job["state"], job["exit_code"], job["command"]) == ("exited", 3, "true")
    assert docker.calls[-1] == ["ps", "-a", "--no-trunc", "--format", PS_FORMAT]

    running = (await rig.api.get(f"/api/docker/{rig.session_url}/containers?all=false")).json()
    assert [c["name"] for c in running] == ["web"]
    # Служебные команды в терминал сессии не попадают
    session = rig.manager.get(rig.session_id)
    assert [e.kind for e in session.buffer.since(0).entries] == ["opened"]


@pytest.mark.parametrize(
    ("setup", "status", "error"),
    [
        (lambda rig, docker: setattr(docker, "daemon", False), 503, "docker_daemon_unavailable"),
        (lambda rig, docker: setattr(docker, "permission", False), 403, "docker_permission_denied"),
        (
            lambda rig, docker: rig.worker.shell.programs.pop("docker"),
            404,
            "docker_not_installed",
        ),
    ],
)
async def test_containers_errors(rig, docker, setup, status, error):
    setup(rig, docker)
    response = await rig.api.get(f"/api/docker/{rig.session_url}/containers")
    assert response.status_code == status, response.text
    assert response.json()["error"] == error


async def test_unknown_session(rig):
    response = await rig.api.get("/api/docker/sessions/0123456789abcdef/containers")
    assert response.status_code == 404
    assert response.json()["error"] == "ssh_session_not_found"


async def test_container_actions(rig, docker):
    base = f"/api/docker/{rig.session_url}/containers"
    stopped = await rig.api.post(f"{base}/web/stop?timeout=2")
    assert stopped.status_code == 200, stopped.text
    assert stopped.json() == {"container": "web", "action": "stop", "output": "web"}
    assert docker.calls[-1] == ["stop", "-t", "2", "web"]
    assert docker.find("web").state == "exited"

    for action, state in (("start", "running"), ("pause", "paused"), ("unpause", "running")):
        response = await rig.api.post(f"{base}/web/{action}")
        assert response.status_code == 200, response.text
        assert docker.find("web").state == state

    await rig.api.post(f"{base}/web/kill")
    assert docker.find("web").status.startswith("Exited (137)")
    by_id = await rig.api.post(f"{base}/7c788158be9c/restart")  # короткий ID
    assert by_id.status_code == 200 and docker.find("job").state == "running"


async def test_action_errors(rig, docker):
    base = f"/api/docker/{rig.session_url}/containers"
    missing = await rig.api.post(f"{base}/nope/start")
    assert missing.status_code == 404
    assert missing.json()["error"] == "docker_container_not_found"

    conflict = await rig.api.post(f"{base}/job/pause")  # job остановлен
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "docker_container_conflict"
    assert "is not running" in conflict.json()["detail"]

    assert (await rig.api.post(f"{base}/web/explode")).status_code == 422
    assert (await rig.api.post(f"{base}/-rf/start")).status_code == 422  # не флаг docker
    assert (await rig.api.post(f"{base}/web/stop?timeout=-1")).status_code == 422


async def test_remove_container(rig, docker):
    base = f"/api/docker/{rig.session_url}/containers"
    running = await rig.api.delete(f"{base}/web")
    assert running.status_code == 409
    assert "force remove" in running.json()["detail"]

    removed = await rig.api.delete(f"{base}/web?force=true&volumes=true")
    assert removed.json() == {"container": "web", "action": "remove", "output": "web"}
    assert docker.calls[-1] == ["rm", "-f", "-v", "web"]
    assert [c.name for c in docker.containers] == ["job"]


async def test_container_logs(rig, docker):
    url = f"/api/docker/{rig.session_url}/containers/web/logs"
    response = await rig.api.get(url, params={"tail": 2})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "container": "web",
        "lines": [
            {"time": WEB_LOGS[1][0], "stream": "stderr", "text": "warning: slow"},
            {"time": WEB_LOGS[2][0], "stream": "stdout", "text": "GET /api 500"},
        ],
        "next_since": WEB_LOGS[2][0],
    }
    assert docker.calls[-1] == ["logs", "--timestamps", "--tail", "2", "web"]

    # Опрос новых строк: since = next_since, уже показанная строка не повторяется
    docker.find("web").logs.append(("2026-11-06T15:42:50.000000004Z", "stdout", "GET /new 200"))
    fresh = (await rig.api.get(url, params={"since": body["next_since"]})).json()
    assert [line["text"] for line in fresh["lines"]] == ["GET /new 200"]
    empty = (await rig.api.get(url, params={"since": fresh["next_since"]})).json()
    assert (empty["lines"], empty["next_since"]) == ([], fresh["next_since"])


async def test_logs_errors(rig):
    base = f"/api/docker/{rig.session_url}/containers"
    missing = await rig.api.get(f"{base}/nope/logs")
    assert missing.status_code == 404
    assert missing.json()["error"] == "docker_container_not_found"
    assert (await rig.api.get(f"{base}/web/logs?tail=100000")).status_code == 422
    assert (
        await rig.api.get(f"{base}/web/logs", params={"since": "1; rm -rf /"})
    ).status_code == 422


async def test_stats(rig, docker):
    response = await rig.api.get(f"/api/docker/{rig.session_url}/stats")
    assert response.status_code == 200, response.text
    (web,) = response.json()  # job остановлен: в docker stats его нет
    assert web == {
        "id": docker.find("web").id,
        "name": "web",
        "cpu_percent": 12.5,
        "memory_used": round(1.5 * 2**20),
        "memory_limit": round(7.834 * 2**30),
        "memory_percent": 0.02,
        "net_rx": 1200,
        "net_tx": 648,
        "block_read": 4_100_000,
        "block_write": 0,
        "pids": 3,
    }


async def test_images(rig):
    response = await rig.api.get(f"/api/docker/{rig.session_url}/images")
    assert response.status_code == 200, response.text
    nginx, unnamed = response.json()
    assert (nginx["repository"], nginx["tag"], nginx["size"]) == ("nginx", "1.27", 12_200_000)
    assert (unnamed["repository"], unnamed["tag"]) == (None, None)
