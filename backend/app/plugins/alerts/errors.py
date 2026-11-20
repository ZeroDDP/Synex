from __future__ import annotations

from app.core.errors import AppError


class AlertNotFoundError(AppError):
    code = "alert_not_found"
    status_code = 404


class AlertActiveError(AppError):
    """Активный алерт нельзя убрать из списка: условие ещё выполняется."""

    code = "alert_active"
    status_code = 409


class AlertRulesCorruptedError(AppError):
    code = "alert_rules_corrupted"
    status_code = 500
