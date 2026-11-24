from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field

from app.external.ipc import HostError
from app.plugins.logs.journal import JournalQuery
from app.plugins.logs.routes import journal_entries
from app.plugins.logs.schemas import UNIT_PATTERN
from app.plugins.monitor.service import metrics_monitor
from app.plugins.ssh.routes import get_ssh_manager


class LogArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unit: str | None = Field(default=None, pattern=UNIT_PATTERN, max_length=256)
    limit: int = Field(default=50, ge=1, le=200)


def known_server(server):
    return any(f"{s.host}:{s.port}" == server for s in get_ssh_manager().sessions())


async def read_service(method, server, args, loaded):
    if method == "metrics.read":
        if "monitor" not in loaded:
            raise HostError("service_unavailable", "Monitor is disabled")
        if args:
            raise HostError("invalid_params", "Unexpected metrics arguments")
        metrics_monitor.touch()
        for item in metrics_monitor.snapshot().servers:
            if item.server == server and item.updated_at is not None:
                return {
                    "server_id": server,
                    "updated_at": item.updated_at,
                    "cpu_percent": item.cpu.percent if item.cpu else None,
                    "memory_percent": item.memory.percent if item.memory else None,
                }
        raise HostError("data_unavailable", "No measurement yet")
    if method != "logs.read":
        raise HostError("permission_denied", "Unknown capability")
    if "logs" not in loaded:
        raise HostError("service_unavailable", "Logs are disabled")
    filters = LogArgs.model_validate(args)
    query = JournalQuery(units=(filters.unit,) if filters.unit else (), since="-1h")
    page = await journal_entries(server, get_ssh_manager(), query, lines=filters.limit)
    entries = []
    size = 64
    for item in page.entries:
        entry = item.model_dump(include={"time", "unit", "level", "message"}, mode="json")
        size += len(json.dumps(entry).encode()) + 2
        if size > 256 * 1024:
            break
        entries.append(entry)
    return {
        "entries": entries,
        "truncated": len(entries) < len(page.entries) or len(entries) == filters.limit,
    }
