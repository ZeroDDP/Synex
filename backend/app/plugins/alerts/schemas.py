from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.plugins.alerts.rules import AlertRule, Metric


class AlertInfo(BaseModel):
    id: str
    server: str = Field(description='"host:port", как в метриках')
    host: str
    port: int
    metric: Metric
    target: str = Field(description='Раздел ("/") у disk, номер карты у gpu_temperature, иначе ""')
    severity: Literal["warning", "critical"]
    status: Literal["active", "resolved"]
    value: float | None = Field(description="Последнее значение (у server: 1 не отвечает)")
    peak: float | None = Field(description="Худшее значение, пока алерт горел")
    threshold: float | None
    title: str = Field(description='Заголовок карточки: "CPU load above 90%"')
    message: str = Field(description="Подробности под заголовком")
    started_at: int = Field(description="Когда загорелся, мс Unix")
    updated_at: int
    resolved_at: int | None
    resolved_reason: str | None = Field(
        description="back_to_normal, server_disconnected или rule_changed"
    )
    acknowledged: bool = Field(description="Пользователь видел алерт (POST .../ack)")


class AlertCounts(BaseModel):
    active: int
    critical: int
    warning: int
    unacknowledged: int
    resolved: int


class AlertList(BaseModel):
    counts: AlertCounts
    alerts: list[AlertInfo]


class AlertRules(BaseModel):
    rules: list[AlertRule]


class AckResult(BaseModel):
    acknowledged: int
    counts: AlertCounts
