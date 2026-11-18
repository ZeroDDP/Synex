"""Что пользователь включил и выключил на ходу: plugins.json в папке данных.

Плагины включаются и выключаются без перезапуска backend (POST /api/plugins/{name}/enable
и /disable). Чтобы после перезапуска набор остался таким, каким его оставил пользователь,
его выбор записывается сюда, а при старте накладывается на PLUGINS_ENABLED.

В файле только отличия от настроек: плагин, который включён в PLUGINS_ENABLED и не
выключен пользователем, в нём не упоминается. Поэтому изменение PLUGINS_ENABLED
действует на всё, что пользователь не переключал сам.

    {"version": 1, "plugins": {"docker": false, "extra": true}}

true: включить, хотя в PLUGINS_ENABLED плагина нет; false: не включать, хотя он там
есть. Файл не обязателен: если его нет или он испорчен, работает набор из настроек.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import structlog

from app.core.files import write_private_file

logger = structlog.get_logger()

FILE_VERSION = 1
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class PluginChoices:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> dict[str, bool]:
        """Выбор пользователя: плагин -> включён ли. Пусто, если файла нет или его не
        прочитать (тогда о причине пишется в лог, а backend запускается как обычно)."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as e:
            logger.warning("Cannot read plugin choices", path=str(self.path), error=str(e))
            return {}
        try:
            content = json.loads(raw)
            plugins = content["plugins"]
            if content["version"] != FILE_VERSION or not isinstance(plugins, dict):
                raise ValueError("unsupported format")
        except (ValueError, KeyError, TypeError) as e:
            logger.warning(
                "Plugin choices file is damaged and ignored: plugins come from PLUGINS_ENABLED",
                path=str(self.path),
                error=str(e),
            )
            return {}
        return {
            name: enabled
            for name, enabled in plugins.items()
            if isinstance(name, str) and _NAME.match(name) and isinstance(enabled, bool)
        }

    def remember(self, changes: dict[str, bool | None]) -> None:
        """Записать выбор: плагин -> включён ли; None убирает запись (плагин снова
        следует настройкам). Если файл не записать, выбор действует до перезапуска."""
        current = self.read()
        updated = dict(current)
        for name, enabled in changes.items():
            if enabled is None:
                updated.pop(name, None)
            else:
                updated[name] = enabled
        if updated == current:
            return
        content = {"version": FILE_VERSION, "plugins": dict(sorted(updated.items()))}
        try:
            write_private_file(self.path, json.dumps(content, indent=2))
        except OSError as e:
            logger.warning(
                "Cannot save plugin choices: they will be lost on restart",
                path=str(self.path),
                error=str(e),
            )
