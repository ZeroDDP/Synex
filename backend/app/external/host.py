from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import structlog

from app.external.ipc import Channel, HostError

log = structlog.get_logger()


class PluginHost:
    def __init__(self, callback, event, command=None):
        binary = "synex_plugin_host.exe" if os.name == "nt" else "synex_plugin_host"
        packaged = Path(sys.executable).resolve().parent / binary
        source = Path(__file__).resolve().parents[3] / "core" / "target" / "release" / binary
        default = packaged if packaged.exists() or getattr(sys, "frozen", False) else source
        self.command = command or [os.environ.get("SYNEX_PLUGIN_HOST_PATH") or str(default)]
        self.callback = callback
        self.event = event
        self.channel = None
        self.supervisor = None
        self.initial = asyncio.Event()

    async def start(self):
        self.supervisor = asyncio.create_task(self._supervise())
        await self.initial.wait()

    async def stop(self):
        if self.supervisor:
            self.supervisor.cancel()
            await asyncio.gather(self.supervisor, return_exceptions=True)

    async def request(self, method, params, timeout=15):
        if self.channel is None:
            raise HostError("host_unavailable", "External plugin host is offline")
        return await self.channel.request(method, params, timeout)

    async def _supervise(self):
        for attempt in range(4):
            try:
                await self._session()
            except FileNotFoundError:
                log.info("External plugin host is not installed")
                return
            except Exception as exc:
                log.warning("External plugin host disconnected", reason=type(exc).__name__)
            finally:
                self.initial.set()
            await asyncio.sleep(2**attempt)

    async def _session(self):
        proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        channel = Channel(proc.stdout, proc.stdin, self.callback, self.event)
        reader = asyncio.create_task(channel.read())
        try:
            hello = await channel.request("hello", {}, 3)
            if hello.get("ipc") != 1:
                raise HostError("host_protocol", "Unsupported IPC version")
            listing = await channel.request("list", {}, 3)
            if not isinstance(listing.get("plugins"), list):
                raise HostError("host_protocol", "Invalid registry")
            self.channel = channel
            self.initial.set()
            await reader
        finally:
            self.channel = None
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), 1)
            except TimeoutError:
                proc.kill()
                await proc.wait()
