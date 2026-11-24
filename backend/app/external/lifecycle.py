from contextlib import asynccontextmanager

from app.external.calls import Calls
from app.external.host import PluginHost
from app.external.packages import Previews
from app.ws import ws_hub


@asynccontextmanager
async def external_plugins(app):
    async def callback(method, params):
        return await calls.callback(method, params)

    def event(name, data):
        if name == "plugin.status":
            if data["state"] != "enabled":
                calls.invalidate(data["id"])
            ws_hub.broadcast("external_plugin.status", {"id": data["id"], "state": data["state"]})

    host = PluginHost(callback, event)
    calls = Calls(host, app.state.plugins)
    app.state.calls = calls
    previews = Previews()
    app.state.plugin_host = host
    app.state.previews = previews
    try:
        await host.start()
        yield
    finally:
        await host.stop()
        await calls.close()
        await previews.close()
