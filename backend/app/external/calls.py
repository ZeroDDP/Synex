from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from uuid import uuid4

from app.core.errors import AppError
from app.external.audit import record
from app.external.ipc import HostError
from app.external.services import read_service
from app.ws import ws_hub


@dataclass
class Call:
    plugin: str
    server: str
    status: str = "running"
    result: object = None
    error: str | None = None
    finished: float = 0
    task: asyncio.Task | None = None
    reads: set[asyncio.Task] = field(default_factory=set)


class Calls:
    def __init__(self, host, manager):
        self.host = host
        self.manager = manager
        self.entries = {}

    def notify(self, key, call):
        ws_hub.broadcast(
            "external_plugin.call",
            {
                "id": call.plugin,
                "call_id": key,
                "status": call.status,
            },
        )

    def get(self, plugin, key):
        call = self.entries.get(key)
        if call and call.finished and time.monotonic() - call.finished >= 600:
            del self.entries[key]
            call = None
        if not call or call.plugin != plugin:
            raise HostError("not_found", "Unknown call")
        return call

    def start(self, plugin, action, server, args):
        now = time.monotonic()
        for key, call in list(self.entries.items()):
            if call.finished and now - call.finished >= 600:
                del self.entries[key]
        if len(self.entries) >= 256:
            oldest = next((k for k, c in self.entries.items() if c.finished), None)
            if oldest is None:
                raise HostError("busy", "Call quota exceeded")
            del self.entries[oldest]
        key = str(uuid4())
        call = self.entries[key] = Call(plugin, server)
        call.task = asyncio.create_task(self._run(key, call, action, args))
        self.notify(key, call)
        return key

    async def _run(self, key, call, action, args):
        try:
            if call.status != "running":
                return
            result = await self.host.request(
                "invoke",
                {
                    "id": call.plugin,
                    "action": action,
                    "server_id": call.server,
                    "call_id": key,
                    "args": args,
                },
                timeout=60,
            )
            if call.status == "running":
                call.result = result
                call.status = "done"
        except AppError as exc:
            if call.status == "running":
                call.status = "failed"
                call.error = exc.code
        except Exception:
            call.status = "failed"
            call.error = "host_protocol"
        finally:
            call.finished = time.monotonic()
            self.notify(key, call)
            record(
                action,
                plugin=call.plugin,
                server=call.server,
                call_id=key,
                outcome=call.error or call.status,
            )

    def invalidate(self, plugin, server=None):
        for key, call in self.entries.items():
            if call.plugin == plugin and (server is None or call.server == server):
                self.mark_cancelled(key, call)

    def mark_cancelled(self, key, call):
        if call.status == "running":
            call.status = "cancelled"
            for read in call.reads:
                read.cancel()
            call.finished = time.monotonic()
            self.notify(key, call)

    async def close(self):
        tasks = [c.task for c in self.entries.values() if c.task and not c.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def callback(self, method, params):
        if method not in {"logs.read", "metrics.read"}:
            raise HostError("permission_denied", "Unknown capability")
        plugin, key, server = params["id"], params["call_id"], params["server_id"]
        call = self.get(plugin, key)

        async def check():
            if server != call.server or call.status != "running":
                raise HostError("permission_denied", "Call scope expired")
            reply = await self.host.request(
                "authorize",
                {
                    "id": plugin,
                    "call_id": key,
                    "server_id": server,
                    "permission": method,
                },
            )
            if reply.get("allowed") is not True or call.status != "running":
                raise HostError("permission_denied", "Capability denied")

        await check()
        read = asyncio.create_task(
            read_service(method, server, params["args"], self.manager.loaded)
        )
        call.reads.add(read)
        try:
            result = await read
        except asyncio.CancelledError:
            if call.status == "cancelled":
                raise HostError("permission_denied", "Call was cancelled") from None
            raise
        finally:
            call.reads.discard(read)
        await check()
        return result
