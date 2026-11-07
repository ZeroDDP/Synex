from __future__ import annotations

from app.plugins.base import BasePlugin
from app.plugins.git.routes import router


class GitPlugin(BasePlugin):
    """Git-репозитории на серверах: /api/git/sessions/{session_id}/... Команды git
    выполняются в SSH-сессии служебными командами и в терминал не попадают."""

    name = "git"
    version = "0.1.0"
    description = "Git-репозитории на серверах: состояние, история, обновление и деплой"
    requires = ("ssh",)
    router = router
