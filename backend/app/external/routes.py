from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.external.audit import record
from app.external.ipc import HostError

router = APIRouter(prefix="/external-plugins", tags=["external plugins"])


class Preview(BaseModel):
    preview_id: str


class Grant(BaseModel):
    permission: Literal["metrics.read", "logs.read"]
    server_id: str


async def change(request, method, params):
    try:
        result = await request.app.state.plugin_host.request(method, params)
    except HostError as exc:
        record(method, plugin=params.get("id"), outcome=exc.code)
        raise
    manifest = result.get("manifest", {}) if isinstance(result, dict) else {}
    record(
        method,
        plugin=params.get("id", manifest.get("id")),
        version=manifest.get("version"),
        server=params.get("server_id"),
        outcome="ok",
    )
    return result


@router.post(
    "/inspect",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {"file": {"type": "string", "format": "binary"}},
                    }
                }
            },
        }
    },
)
async def inspect_package(request: Request):
    return await request.app.state.previews.inspect(request, request.app.state.plugin_host)


async def apply_preview(request, token, plugin=None):
    path, digest = request.app.state.previews.take(token, plugin)
    try:
        params = {"path": str(path), "sha256": digest}
        if plugin is not None:
            params["id"] = plugin
        return await change(request, "update" if plugin else "install", params)
    finally:
        path.unlink(missing_ok=True)


@router.post("/install", status_code=201)
async def install(request: Request, body: Preview):
    return await apply_preview(request, body.preview_id)


@router.get("")
async def list_plugins(request: Request):
    result = await request.app.state.plugin_host.request("list", {})
    return {"plugins": [dict(card, type="external") for card in result["plugins"]]}


@router.get("/{plugin}")
async def get_plugin(request: Request, plugin: str):
    return dict(await request.app.state.plugin_host.request("get", {"id": plugin}), type="external")


@router.post("/{plugin}/enable")
async def enable(request: Request, plugin: str):
    return await change(request, "enable", {"id": plugin})


@router.post("/{plugin}/disable")
async def disable(request: Request, plugin: str):
    return await change(request, "disable", {"id": plugin})


@router.post("/{plugin}/update")
async def update(request: Request, plugin: str, body: Preview):
    return await apply_preview(request, body.preview_id, plugin)


@router.post("/{plugin}/rollback")
async def rollback(request: Request, plugin: str):
    return await change(request, "rollback", {"id": plugin})


@router.delete("/{plugin}")
async def uninstall(request: Request, plugin: str, purge_data: bool = False):
    return await change(request, "uninstall", {"id": plugin, "purge_data": purge_data})
