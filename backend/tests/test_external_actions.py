import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from app.core.errors import register_error_handlers
from app.external import actions, calls, lifecycle
from app.external.host import PluginHost
from app.external.routes import router


async def test_scoped_action_on_real_pipes(monkeypatch):
    peer = [sys.executable, str(Path(__file__).with_name("plugin_host_stub.py"))]
    monkeypatch.setattr(lifecycle, "PluginHost", lambda cb, ev: PluginHost(cb, ev, peer))
    monkeypatch.setattr(actions, "known_server", lambda server: True)

    started, release = asyncio.Event(), asyncio.Event()
    hold = False
    events = []
    monkeypatch.setattr(calls.ws_hub, "broadcast", lambda name, data: events.append((name, data)))

    async def read(method, server, args, loaded):
        if hold:
            started.set()
            await release.wait()
        return {"server_id": server, "cpu_percent": 42}

    monkeypatch.setattr(calls, "read_service", read)
    app = FastAPI()
    register_error_handlers(app)
    app.state.plugins = SimpleNamespace(loaded=["monitor"])
    app.include_router(router)
    app.include_router(actions.router)
    async with (
        lifecycle.external_plugins(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test/external-plugins/"
        ) as client,
    ):
        token = (await client.post("inspect", files={"file": ("x.zip", b"demo")})).json()[
            "preview_id"
        ]
        assert (await client.post("install", json={"preview_id": token})).status_code == 201
        assert (await client.post("local.demo/enable")).is_success
        assert (
            await client.post(
                "local.demo/grants", json={"permission": "metrics.read", "server_id": "A"}
            )
        ).is_success

        async def invoke(server):
            response = await client.post(
                "local.demo/actions/summary", json={"server_id": server, "args": {}}
            )
            assert response.status_code == 202, response.text
            call_id = response.json()["call_id"]
            async with asyncio.timeout(5):
                while True:
                    state = (await client.get("local.demo/calls/" + call_id)).json()
                    if state["status"] != "running":
                        return state
                    await asyncio.sleep(0.01)

        assert (await invoke("A"))["result"]["cpu_percent"] == 42
        assert (await invoke("B"))["error"] == "permission_denied"
        hold = True
        running = asyncio.create_task(invoke("A"))
        await asyncio.wait_for(started.wait(), 4)
        assert (await client.delete("local.demo/grants/metrics.read/A")).is_success
        assert (await invoke("A"))["error"] == "permission_denied"
        assert (await running)["status"] == "cancelled"
        assert any(name == "external_plugin.call" for name, _ in events)
        assert any(name == "external_plugin.status" for name, _ in events)
        token = (await client.post("inspect", files={"file": ("x.zip", b"v2")})).json()[
            "preview_id"
        ]
        assert (await client.post("local.demo/update", json={"preview_id": token})).is_success
        assert (await client.post("local.demo/rollback")).is_success
        assert (await client.delete("local.demo")).is_success
        assert (await client.get("", follow_redirects=True)).json()["plugins"] == []
