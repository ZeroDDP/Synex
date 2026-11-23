import asyncio
import sys
from pathlib import Path

import pytest

from app.external.host import PluginHost
from app.external.ipc import HostError


async def test_duplex_eof_and_timeout():
    async def callback(method, params):
        assert method == "metrics.read"
        return await host.request("list", {})

    host = PluginHost(
        callback,
        lambda *a: None,
        [sys.executable, str(Path(__file__).with_name("plugin_host_stub.py"))],
    )
    await host.start()
    try:
        assert await host.request("invoke", {}) == {"plugins": []}
        with pytest.raises(HostError, match="timed out"):
            await host.request("hang", {}, timeout=0.1)
        with pytest.raises(HostError, match="disconnected"):
            await host.request("disconnect", {})
        async with asyncio.timeout(8):
            while host.channel is None or host.channel.reader.at_eof():
                await asyncio.sleep(0.05)
        assert await host.request("list", {}) == {"plugins": []}
    finally:
        await host.stop()
