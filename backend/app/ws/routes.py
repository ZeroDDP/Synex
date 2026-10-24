from __future__ import annotations

from fastapi import APIRouter, WebSocket

from app.ws.hub import ws_hub

router = APIRouter()


@router.websocket("/ws")
async def websocket_stream(websocket: WebSocket) -> None:
    """Поток данных для UI. Протокол описан в app/ws/hub.py и README."""
    await ws_hub.serve(websocket)
