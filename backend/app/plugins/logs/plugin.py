from __future__ import annotations

from app.plugins.base import BasePlugin
from app.plugins.logs.routes import router
from app.plugins.logs.stream import close_streams


class LogsPlugin(BasePlugin):
    """Логи сервера, выбранного на Dashboard: /api/logs/servers/{host:port}/... Журнал
    systemd (фильтры по юниту, уровню, времени и тексту, страницы старше) и файлы логов
    (/var/log и любой путь), живой лог обоих по WebSocket. Команды journalctl и чтения
    файлов идут служебными командами SSH-сессии и в терминал не попадают."""

    name = "logs"
    version = "0.1.0"
    description = "Журнал systemd и файлы логов серверов"
    requires = ("ssh",)
    router = router

    async def on_unload(self) -> None:
        await close_streams()
