from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.plugins.docker.commands import Container, ContainerStats, Image, LogLine, Port

# Имя или ID контейнера: как в docker (без пробелов и ведущего "-": не станет флагом)
CONTAINER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$"
# --since docker logs: время RFC3339, Unix-время или длительность (10m, 1h30m)
SINCE_PATTERN = r"^[0-9A-Za-z:.+-]{1,64}$"


class DockerInfo(BaseModel):
    status: Literal["ok", "not_installed", "permission_denied", "daemon_unavailable"] = Field(
        ...,
        description=(
            "ok: Docker работает; not_installed: нет docker; permission_denied: "
            "пользователь SSH не в группе docker; daemon_unavailable: демон не запущен"
        ),
    )
    detail: str | None = Field(default=None, description="Текст ошибки, если status не ok")
    client_version: str | None = None
    server_version: str | None = None
    api_version: str | None = None
    os: str | None = None
    arch: str | None = None


class PortInfo(BaseModel):
    container_port: str = Field(..., description='Порт в контейнере: "80" или "9000-9001"')
    protocol: str
    host_ip: str | None = Field(default=None, description="null: порт не опубликован")
    host_port: str | None = None

    @classmethod
    def from_port(cls, port: Port) -> PortInfo:
        return cls(
            container_port=port.container_port,
            protocol=port.protocol,
            host_ip=port.host_ip,
            host_port=port.host_port,
        )


class ContainerInfo(BaseModel):
    id: str
    name: str
    names: list[str]
    image: str
    command: str
    created_at: datetime | None
    state: str = Field(
        ..., description="created, running, paused, restarting, removing, exited, dead"
    )
    status: str = Field(..., description='Как в docker ps: "Up 5 minutes (healthy)"')
    health: str | None = Field(default=None, description="healthy, unhealthy, starting")
    exit_code: int | None = Field(default=None, description="Код выхода остановленного")
    ports: list[PortInfo]
    labels: dict[str, str]
    networks: list[str]
    compose_project: str | None = Field(default=None, description="Проект docker compose")
    compose_service: str | None = None

    @classmethod
    def from_container(cls, container: Container) -> ContainerInfo:
        return cls(
            id=container.id,
            name=container.name,
            names=container.names,
            image=container.image,
            command=container.command,
            created_at=container.created_at,
            state=container.state,
            status=container.status,
            health=container.health,
            exit_code=container.exit_code,
            ports=[PortInfo.from_port(p) for p in container.ports],
            labels=container.labels,
            networks=container.networks,
            compose_project=container.compose_project,
            compose_service=container.compose_service,
        )


ContainerAction = Literal["start", "stop", "restart", "pause", "unpause", "kill"]


class ContainerActionResult(BaseModel):
    container: str
    action: str
    output: str = Field(..., description="Что напечатал docker (обычно имя контейнера)")


class LogLineInfo(BaseModel):
    time: str = Field(..., description="Время строки, RFC3339 с наносекундами (UTC)")
    stream: Literal["stdout", "stderr"]
    text: str

    @classmethod
    def from_line(cls, line: LogLine) -> LogLineInfo:
        return cls(time=line.time, stream=line.stream, text=line.text)


class ContainerLogs(BaseModel):
    container: str
    lines: list[LogLineInfo]
    next_since: str | None = Field(
        ...,
        description="Передайте в since следующего запроса, чтобы получить только новые строки",
    )


class ContainerStatsInfo(BaseModel):
    id: str
    name: str
    cpu_percent: float | None = Field(..., description="100 = одно ядро целиком")
    memory_used: int | None = Field(..., description="Байты")
    memory_limit: int | None
    memory_percent: float | None
    net_rx: int | None = Field(..., description="Принято по сети с запуска, байты")
    net_tx: int | None
    block_read: int | None = Field(..., description="Прочитано с диска, байты")
    block_write: int | None
    pids: int | None

    @classmethod
    def from_stats(cls, stats: ContainerStats) -> ContainerStatsInfo:
        return cls(**stats.__dict__)


class ImageInfo(BaseModel):
    id: str
    repository: str | None = Field(..., description="null: образ без имени (<none>)")
    tag: str | None
    digest: str | None
    created_at: datetime | None
    size: int | None = Field(..., description="Байты")

    @classmethod
    def from_image(cls, image: Image) -> ImageInfo:
        return cls(**image.__dict__)
