from contextlib import asynccontextmanager

from app.external.host import PluginHost
from app.external.ipc import HostError
from app.ws import ws_hub


@asynccontextmanager
async def external_plugins(app):
    async def callback(method, params):
        raise HostError("permission_denied", "Unknown capability")

    def event(name, data):
        if name == "plugin.status":
            ws_hub.broadcast("external_plugin.status", {"id": data["id"], "state": data["state"]})

    host = PluginHost(callback, event)
    app.state.plugin_host = host
    try:
        await host.start()
        yield
    finally:
        await host.stop()
