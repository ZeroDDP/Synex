from __future__ import annotations

from app.plugins.base import BasePlugin
from app.plugins.connections.routes import router


class ConnectionsPlugin(BasePlugin):
    """Сохранённые подключения к серверам: /api/connections (CRUD) и открытие SSH-сессии
    по ним. Пароли хранятся зашифрованными мастер-ключом хранилища ключей."""

    name = "connections"
    version = "0.1.0"
    description = "Сохранённые подключения к серверам"
    requires = ("ssh",)
    router = router
