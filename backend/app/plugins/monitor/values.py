"""Ответ synex_worker на monitor_get_values.

stdout: JSON {"ok": true, "cpu_usage_percent": 2.63, "mem_total_kb": ..., "mem_available_kb":
..., "mem_used_kb": ..., "mem_usage_percent": 60.59, "error": null}. Если команда на
сервере не выполнилась, в stdout текст ("Пустой ответ от сервера ..."), в stderr ошибка
shell. CPU воркер считает от прошлого вызова в той же сессии; первый вызов в сессии
считает от загрузки сервера, поэтому его CPU отбрасывается (см. aggregator.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

KIB = 1024


class MonitorValuesError(Exception):
    """Воркер ответил, но метрик в ответе нет."""


@dataclass(frozen=True)
class Values:
    cpu_percent: float
    mem_total: int  # байты
    mem_used: int
    mem_available: int
    mem_percent: float


def parse_values(response: dict[str, Any]) -> Values:
    stdout = str(response.get("stdout") or "").strip()
    try:
        data = json.loads(stdout)
    except ValueError:
        stderr = str(response.get("stderr") or "").strip()
        detail = stderr or stdout or "empty output"
        raise MonitorValuesError(f"Server did not return metrics: {detail}") from None
    if not isinstance(data, dict):
        raise MonitorValuesError("Server did not return metrics: unexpected JSON")
    if not data.get("ok"):
        raise MonitorValuesError(str(data.get("error") or "Worker reported ok=false"))
    try:
        return Values(
            cpu_percent=round(float(data["cpu_usage_percent"]), 2),
            mem_total=int(data["mem_total_kb"]) * KIB,
            mem_used=int(data["mem_used_kb"]) * KIB,
            mem_available=int(data["mem_available_kb"]) * KIB,
            mem_percent=round(float(data["mem_usage_percent"]), 2),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise MonitorValuesError(f"Incomplete metrics from the server: {e!r}") from e
