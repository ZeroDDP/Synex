from __future__ import annotations

import asyncio
import hashlib
import tempfile
import time
from email import policy
from email.parser import BytesParser
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, Request

from app.external.ipc import HostError

MIB = 1024 * 1024
MAX_FILE = 32 * MIB
TTL = 600


class Previews:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="synex-plugins-")
        self.items = {}
        self.uploading = asyncio.Lock()
        self.sweeper = asyncio.create_task(self._sweep())

    async def _sweep(self):
        while True:
            await asyncio.sleep(30)
            self.expire()

    def expire(self):
        for token, (path, _, deadline, _) in list(self.items.items()):
            if deadline <= time.monotonic():
                path.unlink(missing_ok=True)
                del self.items[token]

    async def close(self):
        self.sweeper.cancel()
        await asyncio.gather(self.sweeper, return_exceptions=True)
        self.tmp.cleanup()

    async def inspect(self, request: Request, host):
        if self.uploading.locked():
            raise HostError("busy", "Another upload is in progress")
        async with self.uploading:
            self.expire()
            if len(self.items) >= 4:
                raise HostError("busy", "Preview quota exceeded")
            token = str(uuid4())
            path = Path(self.tmp.name) / token
            try:
                async with asyncio.timeout(60):
                    digest = await self._receive(request, path)
                result = await host.request("inspect", {"path": str(path)})
                if result["sha256"] != digest:
                    raise HostError("hash_mismatch", "Preview digest mismatch")
                listing = await host.request("list", {})
                manifest = result["manifest"]
                previous = next(
                    (
                        card["manifest"]
                        for card in listing["plugins"]
                        if card["manifest"]["id"] == manifest["id"]
                    ),
                    {},
                )
                result = dict(
                    result,
                    installed_version=previous.get("version"),
                    new_permissions=sorted(
                        set(manifest.get("permissions", [])) - set(previous.get("permissions", []))
                    ),
                )
                self.items[token] = path, digest, time.monotonic() + TTL, manifest["id"]
                return {**result, "preview_id": token, "expires_in": TTL}
            except TimeoutError as exc:
                raise HostError("timeout", "Upload timed out") from exc
            finally:
                if token not in self.items:
                    path.unlink(missing_ok=True)

    @staticmethod
    async def _receive(request, path):
        content_type = request.headers.get("content-type", "")
        if not content_type.lower().startswith("multipart/form-data;"):
            raise HTTPException(415, "Expected multipart/form-data")
        header = ("Content-Type: " + content_type + "\r\n\r\n").encode("latin-1")
        boundary = BytesParser(policy=policy.default).parsebytes(header).get_boundary()
        if not boundary or not boundary.isascii() or len(boundary) > 70:
            raise HTTPException(422, "Invalid multipart boundary")
        opening = b"--" + boundary.encode("ascii")
        delimiter = b"\r\n" + opening
        pending = bytearray()
        headers_done = False
        finished = False
        digest = hashlib.sha256()
        count = 0
        with path.open("xb") as output:
            async for chunk in request.stream():
                pending.extend(chunk)
                if not headers_done:
                    end = pending.find(b"\r\n\r\n")
                    if end > 8192 or (end < 0 and len(pending) > 8192):
                        raise HTTPException(422, "Invalid part headers")
                    if end < 0:
                        continue
                    if not pending.startswith(opening + b"\r\n"):
                        raise HTTPException(422, "Expected file part")
                    fields = BytesParser(policy=policy.default).parsebytes(
                        bytes(pending[len(opening) + 2 : end]) + b"\r\n\r\n"
                    )
                    if fields.get_param("name", header="content-disposition") != "file":
                        raise HTTPException(422, "Expected file field")
                    del pending[: end + 4]
                    headers_done = True
                if not finished:
                    end = pending.find(delimiter)
                    length = end if end >= 0 else max(0, len(pending) - len(delimiter))
                    count += length
                    if count > MAX_FILE:
                        raise HTTPException(413, "Package too large")
                    data = pending[:length]
                    output.write(data)
                    digest.update(data)
                    del pending[:length]
                    if end >= 0:
                        del pending[: len(delimiter)]
                        finished = True
                if finished and len(pending) > 4:
                    raise HTTPException(422, "Expected one file")
        if not finished or pending not in (b"--", b"--\r\n") or not count:
            raise HTTPException(422, "Incomplete upload")
        return digest.hexdigest()

    def take(self, token, plugin=None):
        self.expire()
        entry = self.items.get(token)
        if entry is None:
            raise HostError("expired", "Unknown or expired preview")
        if plugin is not None and plugin != entry[3]:
            raise HostError("conflict", "Package ID differs from update target")
        del self.items[token]
        return entry[:2]
