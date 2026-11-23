from __future__ import annotations

import asyncio
import struct
from uuid import uuid4

import msgpack

from app.core.errors import AppError

LIMIT = 8 * 1024 * 1024


class HostError(AppError):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code
        self.status_code = {
            "host_unavailable": 503,
            "service_unavailable": 503,
            "data_unavailable": 503,
            "timeout": 504,
            "permission_denied": 403,
            "conflict": 409,
            "not_found": 404,
            "plugin_not_found": 404,
            "host_protocol": 502,
            "expired": 410,
            "busy": 429,
        }.get(code, 422)


class Channel:
    def __init__(self, reader, writer, callback, event):
        self.reader = reader
        self.writer = writer
        self.callback = callback
        self.event = event
        self.pending = {}
        self.tasks = set()
        self.lock = asyncio.Lock()

    async def send(self, data):
        raw = msgpack.packb(data, use_bin_type=True)
        if len(raw) > LIMIT:
            raise HostError("frame_size", "Message exceeds IPC limit")
        async with self.lock:
            self.writer.write(struct.pack(">I", len(raw)) + raw)
            await self.writer.drain()

    async def request(self, method, params, timeout=15):
        if len(self.pending) >= 128:
            raise HostError("busy", "Too many pending requests")
        key = str(uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            async with asyncio.timeout(timeout):
                await self.send(dict(type="request", id=key, method=method, params=params))
                return await future
        except TimeoutError as exc:
            raise HostError("timeout", "Plugin host timed out") from exc
        except OSError as exc:
            raise HostError("host_unavailable", "Plugin host disconnected") from exc
        finally:
            self.pending.pop(key, None)
            if future.done() and not future.cancelled():
                future.exception()

    async def reply(self, frame):
        try:
            async with asyncio.timeout(10):
                result = await self.callback(frame["method"], frame["params"])
            body = {"result": result}
        except AppError as exc:
            body = {"error": {"code": exc.code, "message": exc.detail}}
        except Exception:
            body = {"error": {"code": "service_unavailable", "message": "Callback failed"}}
        await self.send(dict(type="response", id=frame["id"], **body))

    async def read(self):
        try:
            while True:
                size = int.from_bytes(await self.reader.readexactly(4), "big")
                if not 0 < size <= LIMIT:
                    raise ValueError("Invalid frame size")
                frame = msgpack.unpackb(await self.reader.readexactly(size), raw=False)
                kind = frame["type"]
                if kind == "response":
                    future = self.pending.get(frame["id"])
                    if future and not future.done():
                        if "error" in frame:
                            error = frame["error"]
                            future.set_exception(HostError(error["code"], error["message"]))
                        else:
                            future.set_result(frame["result"])
                elif kind == "request":
                    if len(self.tasks) >= 32:
                        raise ValueError("Callback queue full")
                    task = asyncio.create_task(self.reply(frame))
                    self.tasks.add(task)
                    task.add_done_callback(self._finished)
                elif kind == "event":
                    self.event(frame["name"], frame["data"])
                else:
                    raise ValueError("Unknown frame type")
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(HostError("host_unavailable", "Plugin host disconnected"))
            tasks = list(self.tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _finished(self, task):
        self.tasks.discard(task)
        if not task.cancelled():
            task.exception()
