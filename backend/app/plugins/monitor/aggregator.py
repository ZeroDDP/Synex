"""Метрики одного сервера: замеры за окно, статус, сводка для UI.

Сервер это host:port. Метрики машины не зависят от сессии, поэтому сколько бы сессий
ни было открыто к серверу, он опрашивается один раз за период, через любую свободную.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass

from app.plugins.monitor.hardware import Hardware
from app.plugins.monitor.values import Values
from app.ws.metrics import (
    CpuMetrics,
    DiskMetrics,
    DiskUsage,
    GpuMetrics,
    MemoryMetrics,
    NetworkMetrics,
    ServerMetrics,
    ServerStatus,
    flow,
    percent_stats,
)


@dataclass(frozen=True)
class Sample:
    at: float  # time.monotonic()
    wall_ms: int  # Unix, мс
    cpu: float | None  # None: первый замер в сессии, CPU в нём от загрузки сервера
    values: Values


def server_key(host: str, port: int) -> str:
    return f"{host.lower()}:{port}"


class ServerState:
    def __init__(self, host: str, port: int, window: float) -> None:
        self.host = host
        self.port = port
        self.window = window
        self.samples: deque[Sample] = deque()
        self.last: Sample | None = None
        self.error: str | None = None
        self.busy = False
        self.polling = False  # опрос уже идёт: следующий не запускается
        self.preferred_session: str | None = None  # опрашиваем через неё, пока свободна
        self.primed: set[str] = set()  # сессии, где у воркера уже есть прошлый замер CPU
        self.hardware: Hardware | None = None  # диски и GPU (hardware.py)
        self.hardware_at = -math.inf  # time.monotonic() последнего замера дисков
        self.hardware_wall_ms: int | None = None

    @property
    def key(self) -> str:
        return server_key(self.host, self.port)

    def record(self, sample: Sample) -> None:
        self.samples.append(sample)
        self.last = sample
        self.error = None
        self.busy = False
        self._trim(sample.at)

    def record_hardware(self, hardware: Hardware, at: float, wall_ms: int) -> None:
        self.hardware = hardware
        self.hardware_at = at
        self.hardware_wall_ms = wall_ms

    def hardware_due(self, now: float, interval: float) -> bool:
        return now - self.hardware_at >= interval

    def record_error(self, message: str) -> None:
        self.error = message

    def record_busy(self) -> None:
        self.busy = True

    def forget_session(self, session_id: str) -> None:
        self.primed.discard(session_id)
        if self.preferred_session == session_id:
            self.preferred_session = None

    def metrics(
        self,
        session_ids: list[str],
        stale_after: float,
        now: float | None = None,
        now_ms: int | None = None,
    ) -> ServerMetrics:
        now = time.monotonic() if now is None else now
        now_ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
        self._trim(now)
        last = self.last
        base = {
            "server": self.key,
            "host": self.host,
            "port": self.port,
            "session_ids": session_ids,
            "busy": self.busy,
            "error": self.error,
            "network": self._network(),
            "disk": self._root_disk(),
            **self._hardware_fields(),
        }
        if last is None:
            return ServerMetrics(**base, status="error" if self.error else "waiting")

        cpu_values = [s.cpu for s in self.samples if s.cpu is not None]
        cpu_avg, cpu_min, cpu_max = percent_stats(cpu_values)
        mem_avg, mem_min, mem_max = percent_stats([s.values.mem_percent for s in self.samples])
        last_cpu = next((s.cpu for s in reversed(self.samples) if s.cpu is not None), None)
        age_ms = max(0, round((now - last.at) * 1000))

        status: ServerStatus
        if self.error:
            status = "error"
        elif age_ms > stale_after * 1000:
            status = "stale"
        elif last_cpu is None:
            status = "warming_up"
        else:
            status = "ok"

        values = last.values
        return ServerMetrics(
            **base,
            status=status,
            updated_at=last.wall_ms,
            age_ms=age_ms,
            cpu=CpuMetrics(percent=last_cpu, avg=cpu_avg, min=cpu_min, max=cpu_max),
            memory=MemoryMetrics(
                total=values.mem_total,
                used=values.mem_used,
                available=values.mem_available,
                percent=values.mem_percent,
                avg=mem_avg,
                min=mem_min,
                max=mem_max,
            ),
        )

    def _network(self) -> NetworkMetrics | None:
        """Сеть по последнему замеру. Скорость сейчас это последняя известная за окно: в
        первом замере через другую сессию воркер её не сообщает (как CPU)."""
        last = self.last
        if last is None or last.values.net_rx_total is None or last.values.net_tx_total is None:
            return None
        rx = [s.values.net_rx_speed for s in self.samples if s.values.net_rx_speed is not None]
        tx = [s.values.net_tx_speed for s in self.samples if s.values.net_tx_speed is not None]
        return NetworkMetrics(
            rx=flow(last.values.net_rx_total, rx[-1] if rx else None, rx),
            tx=flow(last.values.net_tx_total, tx[-1] if tx else None, tx),
        )

    def _root_disk(self) -> DiskUsage | None:
        """Раздел "/": из последнего замера, а если воркер его не сообщает (сборка до
        2026-11-17), из списка дисков."""
        values = self.last.values if self.last is not None else None
        if (
            values is not None
            and values.disk_total is not None
            and values.disk_used is not None
            and values.disk_available is not None
            and values.disk_percent is not None
        ):
            return DiskUsage(
                total=values.disk_total,
                used=values.disk_used,
                available=values.disk_available,
                percent=values.disk_percent,
            )
        disks = self.hardware.disks if self.hardware is not None else []
        root = next((d for d in disks if d.mount == "/"), None)
        if root is None:
            return None
        return DiskUsage(
            total=root.total, used=root.used, available=root.available, percent=root.percent
        )

    def _hardware_fields(self) -> dict[str, object]:
        if self.hardware is None:
            return {}
        # Раздел "/" первым: по нему UI судит о диске сервера
        disks = sorted(self.hardware.disks, key=lambda d: d.mount != "/")
        return {
            "disks": [DiskMetrics(**vars(d)) for d in disks],
            "gpus": [GpuMetrics(**vars(g)) for g in self.hardware.gpus],
            "hardware_updated_at": self.hardware_wall_ms,
        }

    def _trim(self, now: float) -> None:
        # Последний замер остаётся всегда: по нему видны значения устаревших метрик
        while len(self.samples) > 1 and now - self.samples[0].at > self.window:
            self.samples.popleft()
