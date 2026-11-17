"""Формат метрик для UI (сообщение metrics в /ws) и их тестовый источник.

Сообщение metrics приходит каждые 500 мс: MetricsSnapshot с метриками всех серверов, к
которым открыты SSH-сессии. Настоящие данные публикует плагин monitor (опрашивает
synex_worker). FakeMetricsPublisher (WS_FAKE_METRICS=true) публикует тот же формат с
одним тестовым сервером, чтобы UI можно было делать без SSH-сервера. Пока тестовые
метрики включены, плагин monitor свои не публикует: в потоке всегда один источник.

Единицы: байты, байты в секунду, проценты 0–100, время в мс Unix. avg/min/max считаются
за окно window_s секунд.
"""

from __future__ import annotations

import random
import time
from collections import deque
from typing import Literal

import structlog
from pydantic import BaseModel, Field

from app.core.event_bus import event_bus
from app.core.periodic import Periodic

logger = structlog.get_logger()

METRICS_EVENT = "metrics"

GIB = 1024**3

# waiting     замеров ещё нет
# warming_up  память уже есть, CPU появится со второго замера (первый замер в сессии
#             воркер считает от загрузки сервера, он отбрасывается)
# ok          свежие данные
# stale       свежих данных нет дольше MONITOR_STALE_AFTER: все сессии сервера заняты
#             командами (busy) или опрос не отвечает; значения последние известные
# error       последний опрос не удался (error); значения последние известные
ServerStatus = Literal["waiting", "warming_up", "ok", "stale", "error"]


class CpuMetrics(BaseModel):
    percent: float | None = Field(description="Загрузка с прошлого замера, %")
    avg: float | None = None
    min: float | None = None
    max: float | None = None


class MemoryMetrics(BaseModel):
    total: int
    used: int
    available: int
    percent: float
    avg: float | None = Field(default=None, description="Средний percent за окно")
    min: float | None = None
    max: float | None = None


class NetworkFlow(BaseModel):
    total: int = Field(description="Байт с загрузки сервера (счётчик)")
    speed: float | None = Field(
        description="Байт в секунду с прошлого замера; null: прошлого замера ещё нет"
    )
    avg: float | None = Field(default=None, description="Средняя speed за окно")
    min: float | None = None
    max: float | None = None


class NetworkMetrics(BaseModel):
    rx: NetworkFlow = Field(description="Приём")
    tx: NetworkFlow = Field(description="Отправка")


class DiskUsage(BaseModel):
    total: int
    used: int
    available: int
    percent: float = Field(description="Занято от доступного, как в df")


class DiskMetrics(BaseModel):
    mount: str = Field(description="Точка монтирования")
    filesystem: str = Field(description="Устройство, например /dev/sda1")
    total: int
    used: int
    available: int
    percent: float = Field(description="Занято от доступного, как в df")


class GpuMetrics(BaseModel):
    index: int
    name: str
    utilization: float | None = Field(description="Загрузка, %; null: карта не сообщает")
    memory_used: int | None
    memory_total: int | None
    temperature: float | None = Field(description="°C")


class ServerMetrics(BaseModel):
    server: str = Field(description='"host:port": один сервер, сколько бы сессий к нему ни было')
    host: str
    port: int
    session_ids: list[str] = Field(description="Открытые SSH-сессии к этому серверу")
    status: ServerStatus
    busy: bool = Field(default=False, description="Все сессии сервера заняты командами")
    updated_at: int | None = Field(default=None, description="Время последнего замера, мс")
    age_ms: int | None = Field(default=None, description="Сколько прошло с последнего замера")
    cpu: CpuMetrics | None = None
    memory: MemoryMetrics | None = None
    network: NetworkMetrics | None = Field(
        default=None,
        description=(
            "Сеть: все интерфейсы сервера, кроме lo. null: сервер или сборка воркера (до "
            "2026-11-17) её не сообщает"
        ),
    )
    disk: DiskUsage | None = Field(
        default=None,
        description=(
            'Корневой раздел "/", с каждым замером. Со сборкой воркера до 2026-11-17 берётся '
            "из disks и обновляется вместе с ними; null: ещё нет"
        ),
    )
    disks: list[DiskMetrics] | None = Field(
        default=None,
        description=(
            'Все диски (df), первым идёт "/"; раз в MONITOR_HARDWARE_INTERVAL; null: ещё нет'
        ),
    )
    gpus: list[GpuMetrics] | None = Field(
        default=None, description="Видеокарты NVIDIA (nvidia-smi); []: их нет; null: ещё нет"
    )
    hardware_updated_at: int | None = Field(default=None, description="Время замера дисков, мс")
    error: str | None = None


class MetricsSnapshot(BaseModel):
    source: Literal["ssh", "fake"]
    interval_ms: int = Field(description="Как часто приходит сообщение metrics")
    window_s: float = Field(description="Окно, за которое считаются avg/min/max")
    servers: list[ServerMetrics]


def percent_stats(values: list[float]) -> tuple[float | None, float | None, float | None]:
    """avg, min, max списка процентов или скоростей (None, если список пуст)."""
    if not values:
        return None, None, None
    return round(sum(values) / len(values), 2), round(min(values), 2), round(max(values), 2)


def flow(total: int, speed: float | None, speeds: list[float]) -> NetworkFlow:
    """Одно направление сети: счётчик, скорость сейчас и её avg/min/max за окно."""
    avg, low, high = percent_stats(speeds)
    return NetworkFlow(total=total, speed=speed, avg=avg, min=low, max=high)


class _Walk:
    """Случайное блуждание в [low, high] с притяжением к center.

    Значение меняется плавно, без скачков от тика к тику, поэтому графики выглядят
    как настоящие, а не как белый шум.
    """

    PULL = 0.1  # какую долю расстояния до center значение проходит за тик

    def __init__(
        self, rng: random.Random, center: float, low: float, high: float, step: float
    ) -> None:
        self.center = center
        self.value = center
        self._rng = rng
        self._low = low
        self._high = high
        self._step = step

    def next(self) -> float:
        drift = (self.center - self.value) * self.PULL
        noise = self._rng.uniform(-self._step, self._step)
        self.value = min(self._high, max(self._low, self.value + drift + noise))
        return self.value


class FakeMetricsSource:
    """Тестовый сервер на 8 ГиБ RAM и диском 256 ГиБ: CPU, память и скорость сети
    меняются плавно, диск понемногу заполняется."""

    HOST = "demo.synex.local"
    PORT = 22
    MEMORY_TOTAL = 8 * GIB
    DISK_TOTAL = 256 * GIB
    DISK_USED = 100 * GIB
    DISK_GROWTH = 64 * 1024  # байт за тик: занятое место видно растущим
    NETWORK_LIMIT = 12_500_000  # байт в секунду (100 Мбит/с)

    def __init__(
        self, seed: int | None = None, interval: float = 0.5, window: float = 30.0
    ) -> None:
        rng = random.Random(seed)
        self.interval = interval
        self.window = window
        self._cpu = _Walk(rng, center=rng.uniform(10, 40), low=0, high=100, step=8)
        self._memory = _Walk(rng, center=45, low=20, high=95, step=1.5)
        limit = self.NETWORK_LIMIT
        self._rx = _Walk(rng, center=limit * 0.2, low=0, high=limit, step=limit * 0.05)
        self._tx = _Walk(rng, center=limit * 0.04, low=0, high=limit, step=limit * 0.01)
        size = max(1, round(window / interval))
        self._cpu_history: deque[float] = deque(maxlen=size)
        self._memory_history: deque[float] = deque(maxlen=size)
        self._rx_history: deque[float] = deque(maxlen=size)
        self._tx_history: deque[float] = deque(maxlen=size)
        self._rx_total = 3 * GIB
        self._tx_total = GIB
        self._disk_used = self.DISK_USED

    def sample(self) -> MetricsSnapshot:
        cpu = round(self._cpu.next(), 2)
        memory = round(self._memory.next(), 2)
        self._cpu_history.append(cpu)
        self._memory_history.append(memory)
        cpu_avg, cpu_min, cpu_max = percent_stats(list(self._cpu_history))
        mem_avg, mem_min, mem_max = percent_stats(list(self._memory_history))
        used = int(self.MEMORY_TOTAL * memory / 100)
        rx_speed, tx_speed = round(self._rx.next(), 2), round(self._tx.next(), 2)
        self._rx_history.append(rx_speed)
        self._tx_history.append(tx_speed)
        self._rx_total += int(rx_speed * self.interval)
        self._tx_total += int(tx_speed * self.interval)
        self._disk_used = min(self.DISK_TOTAL, self._disk_used + self.DISK_GROWTH)
        disk = DiskUsage(
            total=self.DISK_TOTAL,
            used=self._disk_used,
            available=self.DISK_TOTAL - self._disk_used,
            percent=round(self._disk_used / self.DISK_TOTAL * 100, 2),
        )
        updated_at = time.time_ns() // 1_000_000
        server = ServerMetrics(
            server=f"{self.HOST}:{self.PORT}",
            host=self.HOST,
            port=self.PORT,
            session_ids=[],
            status="ok",
            updated_at=updated_at,
            age_ms=0,
            cpu=CpuMetrics(percent=cpu, avg=cpu_avg, min=cpu_min, max=cpu_max),
            memory=MemoryMetrics(
                total=self.MEMORY_TOTAL,
                used=used,
                available=self.MEMORY_TOTAL - used,
                percent=memory,
                avg=mem_avg,
                min=mem_min,
                max=mem_max,
            ),
            network=NetworkMetrics(
                rx=flow(self._rx_total, rx_speed, list(self._rx_history)),
                tx=flow(self._tx_total, tx_speed, list(self._tx_history)),
            ),
            disk=disk,
            disks=[DiskMetrics(mount="/", filesystem="/dev/sda1", **disk.model_dump())],
            gpus=[],
            hardware_updated_at=updated_at,
        )
        return MetricsSnapshot(
            source="fake",
            interval_ms=round(self.interval * 1000),
            window_s=self.window,
            servers=[server],
        )


class FakeMetricsPublisher:
    """Фоновая задача: раз в interval секунд публикует тестовые метрики в METRICS_EVENT."""

    def __init__(self, interval: float, source: FakeMetricsSource | None = None) -> None:
        self._periodic = Periodic("fake-metrics", interval, self._publish)  # проверит interval
        self._source = source or FakeMetricsSource(interval=interval)
        self.interval = interval

    @property
    def running(self) -> bool:
        return self._periodic.running

    def start(self) -> None:
        self._periodic.start()

    async def stop(self) -> None:
        await self._periodic.stop()

    async def _publish(self) -> None:
        await event_bus.publish(METRICS_EVENT, self._source.sample())
