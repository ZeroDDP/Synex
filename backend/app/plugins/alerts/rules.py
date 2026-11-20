"""Правила алертов: пороги по метрикам серверов и сколько условие должно держаться.

Правила одни на все серверы. Хранятся в alerts.json рядом с keys.json (ALERTS_PATH);
пока файла нет, действуют правила по умолчанию (DEFAULT_RULES). Формат файла:

    {"version": 1, "rules": [{"metric", "warning", "critical", "duration_s", "enabled"}]}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import structlog
from pydantic import BaseModel, Field, ValidationError, model_validator

from app.core.files import write_private_file
from app.plugins.alerts.errors import AlertRulesCorruptedError

logger = structlog.get_logger()

FILE_VERSION = 1

# cpu, memory: проценты; disk: занятость каждого раздела, %; gpu_temperature: °C;
# server: сервер не отвечает (опрос с ошибкой или нет свежих данных, хотя сессии свободны)
Metric = Literal["cpu", "memory", "disk", "gpu_temperature", "server"]
METRICS: tuple[Metric, ...] = ("cpu", "memory", "disk", "gpu_temperature", "server")


class AlertRule(BaseModel):
    metric: Metric
    warning: float | None = Field(
        default=None, ge=0, description="Порог предупреждения (у server не используется)"
    )
    critical: float | None = Field(
        default=None, ge=0, description="Порог критического алерта (у server не используется)"
    )
    duration_s: float = Field(
        default=60, ge=0, le=86_400, description="Сколько секунд условие должно держаться"
    )
    enabled: bool = True

    @model_validator(mode="after")
    def _check(self) -> AlertRule:
        if self.metric == "server":
            return self
        if self.warning is None and self.critical is None:
            raise ValueError(f"rule '{self.metric}' needs a warning or a critical threshold")
        if self.warning is not None and self.critical is not None and self.warning > self.critical:
            raise ValueError(f"rule '{self.metric}': warning must not be above critical")
        return self


DEFAULT_RULES: tuple[AlertRule, ...] = (
    AlertRule(metric="cpu", warning=90, critical=95, duration_s=60),
    AlertRule(metric="memory", warning=90, critical=95, duration_s=60),
    AlertRule(metric="disk", warning=90, critical=95, duration_s=0),
    AlertRule(metric="gpu_temperature", warning=80, critical=90, duration_s=60),
    AlertRule(metric="server", duration_s=30),
)


def normalize(rules: list[AlertRule]) -> list[AlertRule]:
    """Одно правило на метрику (последнее выигрывает), в порядке METRICS. Метрика без
    правила: правило по умолчанию."""
    by_metric = {rule.metric: rule for rule in DEFAULT_RULES}
    by_metric.update({rule.metric: rule for rule in rules})
    return [by_metric[metric] for metric in METRICS]


class RulesStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[AlertRule]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return list(DEFAULT_RULES)
        except OSError as e:
            logger.warning("Cannot read alert rules, using defaults", error=str(e))
            return list(DEFAULT_RULES)
        try:
            content = json.loads(raw)
            if content["version"] != FILE_VERSION:
                raise ValueError("unsupported version")
            return normalize([AlertRule.model_validate(item) for item in content["rules"]])
        except (ValueError, KeyError, TypeError, ValidationError) as e:
            raise AlertRulesCorruptedError(
                f"{self.path} is damaged ({e}). Fix it or reset the rules "
                "(DELETE /api/alerts/rules)"
            ) from None

    def save(self, rules: list[AlertRule]) -> None:
        content = {
            "version": FILE_VERSION,
            "rules": [rule.model_dump() for rule in rules],
        }
        write_private_file(self.path, json.dumps(content, indent=2))

    def reset(self) -> None:
        self.path.unlink(missing_ok=True)
