from __future__ import annotations

from app.keystore import get_keystore
from app.plugins.base import BasePlugin
from app.plugins.keys.routes import router


class KeysPlugin(BasePlugin):
    """Хранилище SSH-ключей: /api/keys (CRUD) и /api/keys/generate."""

    name = "keys"
    version = "0.1.0"
    router = router

    async def on_load(self) -> None:
        # Временные файлы ключей, оставшиеся после аварийного завершения
        get_keystore().cleanup_runtime()

    async def on_unload(self) -> None:
        get_keystore().cleanup_runtime()
