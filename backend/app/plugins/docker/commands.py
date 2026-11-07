"""Команды docker CLI на сервере и разбор их вывода.

Воркер Docker не знает, поэтому плагин выполняет на сервере обычный `docker` через
SSH-сессию и разбирает вывод `--format` (Go-шаблон с {{json ...}}: каждая строка это
JSON-объект, кавычки и спецсимволы уже экранированы docker).

Нужен Docker 20.10 или новее (поле .State в `docker ps`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

# Поля docker ps без .Size: размер docker считает долго (обходит файлы контейнера)
PS_FORMAT = (
    '{"id":{{json .ID}},"names":{{json .Names}},"image":{{json .Image}},'
    '"command":{{json .Command}},"created_at":{{json .CreatedAt}},"state":{{json .State}},'
    '"status":{{json .Status}},"ports":{{json .Ports}},"labels":{{json .Labels}},'
    '"networks":{{json .Networks}}}'
)
VERSION_FORMAT = "{{json .}}"
STATS_FORMAT = "{{json .}}"
# Без .Containers, .SharedSize, .UniqueSize: их docker считает отдельно
IMAGES_FORMAT = (
    '{"id":{{json .ID}},"repository":{{json .Repository}},"tag":{{json .Tag}},'
    '"digest":{{json .Digest}},"created_at":{{json .CreatedAt}},"size":{{json .Size}}}'
)

COMPOSE_PROJECT = "com.docker.compose.project"
COMPOSE_SERVICE = "com.docker.compose.service"

_SIZE_RE = re.compile(r"^([0-9.]+)\s*([A-Za-z]*)$")
# docker stats: память в двоичных единицах (MiB), сеть и диск в десятичных (MB)
_UNITS = {
    "": 1, "b": 1,
    "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4, "pb": 1000**5,
    "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4, "pib": 1024**5,
}  # fmt: skip
_EXIT_RE = re.compile(r"^Exited \((-?\d+)\)")
# Время строки docker logs --timestamps: RFC3339 с наносекундами, всегда UTC
_LOG_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
_HEALTH_RE = re.compile(r"\((healthy|unhealthy|health: starting)\)")


@dataclass(frozen=True)
class DockerVersion:
    client_version: str | None
    server_version: str | None
    api_version: str | None
    os: str | None
    arch: str | None


@dataclass(frozen=True)
class Port:
    container_port: str  # "80" или диапазон "9000-9001"
    protocol: str  # tcp, udp, sctp
    host_ip: str | None = None  # None: порт не опубликован
    host_port: str | None = None


@dataclass(frozen=True)
class Container:
    id: str
    name: str
    image: str
    command: str
    created_at: datetime | None
    state: str  # created, running, paused, restarting, removing, exited, dead
    status: str  # как в docker ps: "Up 5 minutes (healthy)"
    health: str | None  # healthy, unhealthy, starting
    exit_code: int | None
    ports: list[Port]
    labels: dict[str, str]
    networks: list[str]
    compose_project: str | None
    compose_service: str | None
    names: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ContainerStats:
    id: str
    name: str
    cpu_percent: float | None  # 100% = одно ядро, на нескольких ядрах бывает больше
    memory_used: int | None  # байты
    memory_limit: int | None
    memory_percent: float | None
    net_rx: int | None
    net_tx: int | None
    block_read: int | None
    block_write: int | None
    pids: int | None


@dataclass(frozen=True)
class Image:
    id: str
    repository: str | None  # None: <none> (образ без имени)
    tag: str | None
    digest: str | None
    created_at: datetime | None
    size: int | None


def parse_stats(stdout: str) -> list[ContainerStats]:
    """`docker stats --no-stream --format '{{json .}}'` (только запущенные контейнеры)."""
    stats = []
    for line in stdout.splitlines():
        row = _json_object(line)
        if row is None or not (row.get("ID") or row.get("Container")):
            continue
        memory_used, memory_limit = _pair(row.get("MemUsage"))
        net_rx, net_tx = _pair(row.get("NetIO"))
        block_read, block_write = _pair(row.get("BlockIO"))
        pids = str(row.get("PIDs") or "")
        stats.append(
            ContainerStats(
                id=str(row.get("ID") or row.get("Container")),
                name=str(row.get("Name") or ""),
                cpu_percent=_percent(row.get("CPUPerc")),
                memory_used=memory_used,
                memory_limit=memory_limit,
                memory_percent=_percent(row.get("MemPerc")),
                net_rx=net_rx,
                net_tx=net_tx,
                block_read=block_read,
                block_write=block_write,
                pids=int(pids) if pids.isdigit() else None,
            )
        )
    return stats


def parse_images(stdout: str) -> list[Image]:
    images = []
    for line in stdout.splitlines():
        row = _json_object(line)
        if row is None or not row.get("id"):
            continue
        images.append(
            Image(
                id=str(row["id"]),
                repository=_none(row.get("repository")),
                tag=_none(row.get("tag")),
                digest=_none(row.get("digest")),
                created_at=parse_created_at(str(row.get("created_at") or "")),
                size=parse_size(str(row.get("size") or "")),
            )
        )
    return images


def parse_size(text: str) -> int | None:
    """ "1.07MiB", "12.2MB", "0B" -> байты; "--" и непонятное -> None."""
    match = _SIZE_RE.match(text.strip())
    if match is None:
        return None
    factor = _UNITS.get(match.group(2).lower())
    try:
        return round(float(match.group(1)) * factor) if factor is not None else None
    except ValueError:
        return None


def _pair(value: Any) -> tuple[int | None, int | None]:
    """ "1.07MiB / 7.834GiB" -> (байты, байты)."""
    first, sep, second = str(value or "").partition("/")
    if not sep:
        return None, None
    return parse_size(first), parse_size(second)


def _percent(value: Any) -> float | None:
    try:
        return float(str(value or "").strip().removesuffix("%"))
    except ValueError:
        return None


def _none(value: Any) -> str | None:
    text = str(value or "")
    return None if text in ("", "<none>") else text


@dataclass(frozen=True)
class LogLine:
    time: str  # как у docker: 2026-11-06T16:05:18.637602832Z
    stream: Literal["stdout", "stderr"]
    text: str


def parse_logs(stdout: str, stderr: str, since: str | None = None) -> list[LogLine]:
    """Вывод `docker logs --timestamps`: строки контейнера из stdout и stderr docker,
    по времени. since: время последней уже показанной строки, она и более ранние
    пропускаются (docker --since включает строку с этим временем)."""
    lines = [*_log_lines(stdout, "stdout"), *_log_lines(stderr, "stderr")]
    # Время в одном формате (UTC, 9 знаков): строки сортируются как время
    lines.sort(key=lambda line: line.time)
    if since is not None and is_log_time(since):
        lines = [line for line in lines if line.time > since]
    return lines


def is_log_time(value: str) -> bool:
    return _LOG_TIME_RE.match(value) is not None


def _log_lines(text: str, stream: Literal["stdout", "stderr"]) -> list[LogLine]:
    lines: list[LogLine] = []
    for raw in text.split("\n"):  # не splitlines: \r внутри строки (прогресс) не разрыв
        time, sep, rest = raw.partition(" ")
        if sep and is_log_time(time):
            lines.append(LogLine(time, stream, rest.removesuffix("\r")))
        elif lines and raw:
            # Продолжение предыдущей строки (например, после \r без \n)
            previous = lines[-1]
            lines[-1] = LogLine(previous.time, stream, f"{previous.text}\n{raw}")
    return lines


def parse_version(stdout: str) -> DockerVersion:
    """`docker version --format '{{json .}}'`. Server: null, если демон недоступен."""
    data = _json_object(stdout.strip()) or {}
    client = data.get("Client") or {}
    server = data.get("Server") or {}
    return DockerVersion(
        client_version=client.get("Version"),
        server_version=server.get("Version"),
        api_version=server.get("ApiVersion") or client.get("ApiVersion"),
        os=server.get("Os") or client.get("Os"),
        arch=server.get("Arch") or client.get("Arch"),
    )


def parse_containers(stdout: str) -> list[Container]:
    containers = []
    for line in stdout.splitlines():
        row = _json_object(line)
        if row is None or not row.get("id"):
            continue
        names = [n for n in str(row.get("names") or "").split(",") if n]
        labels = parse_labels(str(row.get("labels") or ""))
        status = str(row.get("status") or "")
        exit_match = _EXIT_RE.match(status)
        health = _HEALTH_RE.search(status)
        containers.append(
            Container(
                id=str(row["id"]),
                name=names[0] if names else str(row["id"])[:12],
                names=names,
                image=str(row.get("image") or ""),
                command=_unquote(str(row.get("command") or "")),
                created_at=parse_created_at(str(row.get("created_at") or "")),
                state=str(row.get("state") or _state_from_status(status)),
                status=status,
                health=health.group(1).removeprefix("health: ") if health else None,
                exit_code=int(exit_match.group(1)) if exit_match else None,
                ports=parse_ports(str(row.get("ports") or "")),
                labels=labels,
                networks=[n for n in str(row.get("networks") or "").split(",") if n],
                compose_project=labels.get(COMPOSE_PROJECT),
                compose_service=labels.get(COMPOSE_SERVICE),
            )
        )
    return containers


def parse_labels(text: str) -> dict[str, str]:
    """ "a=1,b=2" (значения с запятыми docker не экранирует: склеиваются с предыдущим)."""
    labels: dict[str, str] = {}
    last: str | None = None
    for part in text.split(","):
        key, sep, value = part.partition("=")
        if sep and key:
            labels[key] = value
            last = key
        elif last is not None and part:
            labels[last] += "," + part
    return labels


def parse_ports(text: str) -> list[Port]:
    """ "0.0.0.0:8080->80/tcp, [::]:8080->80/tcp, 9000/udp"."""
    ports = []
    for item in (p.strip() for p in text.split(",")):
        if not item:
            continue
        published, arrow, target = item.rpartition("->")
        container, _, protocol = (target if arrow else item).partition("/")
        host_ip = host_port = None
        if arrow:
            host_ip, _, host_port = published.rpartition(":")
            host_ip = host_ip.strip("[]") or None
        ports.append(Port(container, protocol or "tcp", host_ip, host_port or None))
    return ports


def parse_created_at(text: str) -> datetime | None:
    """ "2026-11-06 22:42:34 +0700 +07" (последнее слово: имя зоны, его нет в strptime)."""
    parts = text.split()
    if len(parts) < 3:
        return None
    try:
        return datetime.strptime(" ".join(parts[:3]), "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return None


def _state_from_status(status: str) -> str:
    """Docker старше 20.10 не знает .State: восстанавливаем по .Status."""
    if status.startswith("Up"):
        return "paused" if "(Paused)" in status else "running"
    if status.startswith("Exited"):
        return "exited"
    if status.startswith("Restarting"):
        return "restarting"
    return status.split(" ")[0].lower() if status else "unknown"


def _unquote(command: str) -> str:
    if len(command) >= 2 and command[0] == command[-1] == '"':
        return command[1:-1]
    return command


def _json_object(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None
