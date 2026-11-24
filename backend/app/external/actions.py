from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.external.ipc import HostError
from app.external.routes import Grant, change, get_plugin
from app.external.services import known_server

router = APIRouter(prefix="/external-plugins", tags=["external plugins"])


class Invoke(BaseModel):
    server_id: str = Field(min_length=1, max_length=512)
    args: dict[str, Any]


def check_object(args, schema):
    """Preflight common object constraints; the host validates the complete guest schema."""
    if set(schema.get("required", ())) - args.keys():
        raise HostError("invalid_params", "Missing required action arguments")
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False and args.keys() - properties.keys():
        raise HostError("invalid_params", "Unknown action arguments")
    types = {
        "string": (str,),
        "boolean": (bool,),
        "integer": (int,),
        "number": (int, float),
        "array": (list,),
        "object": (dict,),
        "null": (type(None),),
    }
    for name, value in args.items():
        spec = properties.get(name, {})
        expected = spec.get("type") if isinstance(spec, dict) else None
        if isinstance(expected, str) and expected in types and type(value) not in types[expected]:
            raise HostError("invalid_params", f"Invalid type for {name}")


@router.post("/{plugin}/actions/{action}", status_code=202)
async def invoke(request: Request, plugin: str, action: str, body: Invoke):
    card = await get_plugin(request, plugin)
    if card["state"] != "enabled":
        raise HostError("conflict", "Plugin is not enabled")
    for item in card["manifest"]["actions"]:
        if item["id"] == action:
            check_object(body.args, item["input_schema"])
            key = request.app.state.calls.start(plugin, action, body.server_id, body.args)
            return {"call_id": key}
    raise HostError("not_found", "Unknown action")


@router.get("/{plugin}/calls/{key}")
async def status(request: Request, plugin: str, key: str):
    call = request.app.state.calls.get(plugin, key)
    return {"call_id": key, "status": call.status, "result": call.result, "error": call.error}


@router.delete("/{plugin}/calls/{key}")
async def cancel(request: Request, plugin: str, key: str):
    calls = request.app.state.calls
    call = calls.get(plugin, key)
    if call.status == "running":
        calls.mark_cancelled(key, call)
        await change(request, "cancel", {"call_id": key})
    return await status(request, plugin, key)


@router.post("/{plugin}/grants")
async def grant(request: Request, plugin: str, body: Grant):
    card = await get_plugin(request, plugin)
    if body.permission not in card["manifest"]["permissions"]:
        raise HostError("permission_denied", "Undeclared permission")
    if not known_server(body.server_id):
        raise HostError("not_found", "Unknown server")
    return await change(request, "grant", {"id": plugin, **body.model_dump()})


@router.delete("/{plugin}/grants/{permission}/{server_id}")
async def revoke(request: Request, plugin: str, permission: str, server_id: str):
    request.app.state.calls.invalidate(plugin, server_id)
    return await change(
        request,
        "revoke",
        {
            "id": plugin,
            "permission": permission,
            "server_id": server_id,
        },
    )
