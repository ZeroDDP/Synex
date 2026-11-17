"""Ответ synex_worker на monitor_get_values.

stdout: JSON {"ok": true, "cpu_usage_percent": 2.63, "mem_total_kb": ..., "mem_available_kb":
..., "mem_used_kb": ..., "mem_usage_percent": 60.59, "error": null}. Если команда на
сервере не выполнилась, в stdout текст ("Пустой ответ от сервера ..."), в stderr ошибка
shell. CPU воркер считает от прошлого вызова в той же сессии; первый вызов в сессии
считает от загрузки сервера, поэтому его CPU отбрасывается (см. aggregator.py).

С 2026-11-17 в том же JSON есть сеть и корневой раздел:

* net_rx_bytes_total, net_tx_bytes_total: счётчики принятых и отправленных байт с загрузки
  сервера, сумма по всем интерфейсам, кроме lo (/proc/net/dev);
* net_rx_bytes_per_sec, net_tx_bytes_per_sec: скорость с прошлого вызова в той же сессии;
  в первом вызове сессии null;
* disk_total_kb, disk_used_kb, disk_available_kb, disk_usage_percent: раздел "/"
  (`df -P -k /`).

Каждое из этих полей может быть null: на сервере нет /proc/net/dev или df. Сборки воркера
до 2026-11-17 этих полей не присылают. В обоих случаях в Values остаётся None, а CPU и
память разбираются как раньше.
"""

from __future__ import annotations

import json
import math
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
    # Сеть: счётчики в байтах с загрузки сервера и скорость в байтах в секунду. None:
    # воркер их не сообщил; у скорости ещё и первый вызов в сессии
    net_rx_total: int | None = None
    net_tx_total: int | None = None
    net_rx_speed: float | None = None
    net_tx_speed: float | None = None
    # Корневой раздел "/", байты. None: воркер его не сообщил
    disk_total: int | None = None
    disk_used: int | None = None
    disk_available: int | None = None
    disk_percent: float | None = None


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
            **_network(data),
            **_root_disk(data),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise MonitorValuesError(f"Incomplete metrics from the server: {e!r}") from e


def _network(data: dict[str, Any]) -> dict[str, Any]:
    """Сеть есть, если пришли оба счётчика. Скорости нет в первом вызове сессии."""
    rx, tx = _count(data.get("net_rx_bytes_total")), _count(data.get("net_tx_bytes_total"))
    if rx is None or tx is None:
        return {}
    return {
        "net_rx_total": rx,
        "net_tx_total": tx,
        "net_rx_speed": _amount(data.get("net_rx_bytes_per_sec")),
        "net_tx_speed": _amount(data.get("net_tx_bytes_per_sec")),
    }


def _root_disk(data: dict[str, Any]) -> dict[str, Any]:
    """Раздел есть, если пришли размер, занятое и свободное."""
    total, used, available = (
        _count(data.get(key)) for key in ("disk_total_kb", "disk_used_kb", "disk_available_kb")
    )
    if total is None or used is None or available is None:
        return {}
    percent = _amount(data.get("disk_usage_percent"))
    if percent is None:
        # Как в df: занято от доступного пользователю (без резерва root)
        percent = used / (used + available) * 100 if used + available else 0.0
    return {
        "disk_total": total * KIB,
        "disk_used": used * KIB,
        "disk_available": available * KIB,
        "disk_percent": round(min(percent, 100.0), 2),
    }


def _count(value: Any) -> int | None:
    """Неотрицательное целое из необязательного поля; None, если его нет или там не число."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return int(value)


def _amount(value: Any) -> float | None:
    """Неотрицательное число из необязательного поля, до сотых."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return round(float(value), 2)
