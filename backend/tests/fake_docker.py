"""Фейковый docker CLI для эмулятора shell (FakeShell.programs["docker"]).

Повторяет вывод Docker 29 для команд плагина docker: `--format` с {{json .Поле}}
рендерится из полей контейнера, ошибки и коды как у настоящего docker. Режимы:
daemon=False (демон не запущен), permission=False (пользователь не в группе docker).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

DAEMON_DOWN = (
    "failed to connect to the docker API at unix:///var/run/docker.sock; check if the path "
    "is correct and if the daemon is running: dial unix /var/run/docker.sock: connect: no "
    "such file or directory"
)
PERMISSION_DENIED = (
    "permission denied while trying to connect to the docker API at unix:///var/run/docker.sock"
)
CLIENT = {"Version": "29.9.1", "ApiVersion": "1.56", "Os": "linux", "Arch": "amd64"}
SERVER = {"Version": "29.9.1", "ApiVersion": "1.56", "Os": "linux", "Arch": "amd64"}

_FIELD_RE = re.compile(r"\{\{json \.(\w*)\}\}")


@dataclass
class FakeContainer:
    id: str
    name: str
    image: str = "nginx:1.27"
    command: str = "\"nginx -g 'daemon off;'\""
    created_at: str = "2026-11-06 22:42:34 +0700 +07"
    state: str = "running"
    status: str = "Up 5 minutes"
    ports: str = ""
    labels: str = ""
    networks: str = "bridge"
    logs: list[tuple[str, str, str]] = field(default_factory=list)  # (время, поток, текст)
    stats: dict[str, str] = field(
        default_factory=lambda: {
            "CPUPerc": "12.50%",
            "MemUsage": "1.5MiB / 7.834GiB",
            "MemPerc": "0.02%",
            "NetIO": "1.2kB / 648B",
            "BlockIO": "4.1MB / 0B",
            "PIDs": "3",
        }
    )

    def row(self) -> dict[str, Any]:
        return {
            "ID": self.id,
            "Names": self.name,
            "Image": self.image,
            "Command": self.command,
            "CreatedAt": self.created_at,
            "State": self.state,
            "Status": self.status,
            "Ports": self.ports,
            "Labels": self.labels,
            "Networks": self.networks,
        }


def render(template: str, row: dict[str, Any]) -> str:
    """Подмножество Go-шаблона docker: {{json .}} и {{json .Поле}}."""
    return _FIELD_RE.sub(lambda m: json.dumps(row[m.group(1)] if m.group(1) else row), template)


@dataclass
class FakeImage:
    id: str
    repository: str
    tag: str
    size: str = "12.2MB"
    digest: str = "<none>"
    created_at: str = "2026-04-17 06:53:26 +0700 +07"

    def row(self) -> dict[str, Any]:
        return {
            "ID": self.id,
            "Repository": self.repository,
            "Tag": self.tag,
            "Digest": self.digest,
            "CreatedAt": self.created_at,
            "Size": self.size,
        }


@dataclass
class FakeDocker:
    containers: list[FakeContainer] = field(default_factory=list)
    images: list[FakeImage] = field(default_factory=list)
    daemon: bool = True
    permission: bool = True
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, args: list[str], cwd: str, env: dict[str, str]) -> tuple[str, str, int]:
        self.calls.append(args)
        command, options, rest = _parse(args)
        if command == "version":
            server = SERVER if self.daemon and self.permission else None
            out = render(options["--format"], {"Client": CLIENT, "Server": server}) + "\n"
            error = self._daemon_error()
            return out, error + "\n" if error else "", 1 if error else 0
        error = self._daemon_error()
        if error:
            return "", error + "\n", 1
        if command == "ps":
            return self._ps(options), "", 0
        if command == "stats":  # --no-stream: один замер запущенных контейнеров
            running = [c for c in self.containers if c.state in ("running", "paused")]
            rows = [{"ID": c.id, "Container": c.id, "Name": c.name, **c.stats} for c in running]
            return "".join(render(options["--format"], row) + "\n" for row in rows), "", 0
        if command == "images":
            rows = [image.row() for image in self.images]
            return "".join(render(options["--format"], row) + "\n" for row in rows), "", 0
        if command == "logs":
            return self._logs(options, rest[0])
        if command in _ACTIONS or command == "rm":
            return self._act(command, options, rest[0])
        return "", f"docker: unknown command: docker {command}\n", 1

    def find(self, name: str) -> FakeContainer | None:
        return next((c for c in self.containers if name in (c.name, c.id, c.id[:12])), None)

    def _daemon_error(self) -> str | None:
        if not self.permission:
            return PERMISSION_DENIED
        if not self.daemon:
            return DAEMON_DOWN
        return None

    def _act(self, command: str, options: dict[str, Any], name: str) -> tuple[str, str, int]:
        container = self.find(name)
        if container is None:
            return "", f"Error response from daemon: No such container: {name}\n", 1
        state = container.state
        error = None
        if command in ("pause", "kill") and state != "running":
            error = f"container {container.id} is not running"
            if command == "kill":
                error = f"cannot kill container: {name}: {error}"
        elif command == "unpause" and state != "paused":
            error = f"Container {container.id} is not paused"
        elif command == "rm" and state in ("running", "paused") and "-f" not in options:
            error = (
                f'cannot remove container "{container.name}": container is running: stop the '
                "container before removing or force remove"
            )
        if error is not None:
            return "", f"Error response from daemon: {error}\n", 1

        if command == "rm":
            self.containers.remove(container)
        else:
            container.state, container.status = _ACTIONS[command]
        return f"{name}\n", "", 0

    def _logs(self, options: dict[str, Any], name: str) -> tuple[str, str, int]:
        container = self.find(name)
        if container is None:
            return "", f"Error response from daemon: No such container: {name}\n", 1
        entries = container.logs
        if "--since" in options:  # как docker: строка с этим временем тоже попадает
            entries = [e for e in entries if e[0] >= options["--since"]]
        tail = int(options.get("--tail", len(entries)))
        entries = entries[len(entries) - tail :] if tail else []
        prefix = "--timestamps" in options
        out = {"stdout": "", "stderr": ""}
        for time, stream, text in entries:
            out[stream] += f"{time} {text}\n" if prefix else f"{text}\n"
        return out["stdout"], out["stderr"], 0

    def _ps(self, options: dict[str, Any]) -> str:
        rows = [c for c in self.containers if "-a" in options or c.state in ("running", "paused")]
        return "".join(render(options["--format"], c.row()) + "\n" for c in rows)


# Действие -> (state, status) после него
_ACTIONS = {
    "start": ("running", "Up Less than a second"),
    "restart": ("running", "Up Less than a second"),
    "unpause": ("running", "Up 5 minutes"),
    "stop": ("exited", "Exited (0) Less than a second ago"),
    "kill": ("exited", "Exited (137) Less than a second ago"),
    "pause": ("paused", "Up 5 minutes (Paused)"),
}
_VALUE_OPTIONS = {"--format", "--tail", "--since", "-t", "--time"}


def _parse(args: list[str]) -> tuple[str, dict[str, Any], list[str]]:
    """docker <command> [опции] [аргументы] -> (command, {опция: значение}, аргументы)."""
    command, options, rest = args[0], {}, []
    items = iter(args[1:])
    for item in items:
        if item in _VALUE_OPTIONS:
            options[item] = next(items)
        elif item.startswith("-"):
            options[item] = True
        else:
            rest.append(item)
    return command, options, rest
