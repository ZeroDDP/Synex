"""Формат метрик для UI (сообщение metrics в /ws) и их тестовый источник.

Сообщение metrics приходит каждые 500 мс: MetricsSnapshot с метриками всех серверов, к
которым открыты SSH-сессии. Настоящие данные публикует плагин monitor (опрашивает
synex_worker). FakeMetricsPublisher (WS_FAKE_METRICS=true) публикует тот же формат с
одним тестовым сервером, чтобы UI можно было делать без SSH-сервера. Пока тестовые
метрики включены, плагин monitor свои не публикует: в потоке всегда один источник.

Единицы: байты, проценты 0–100, время в мс Unix. avg/min/max считаются за окно
window_s секунд.
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
    error: str | None = None


class MetricsSnapshot(BaseModel):
    source: Literal["ssh", "fake"]
    interval_ms: int = Field(description="Как часто приходит сообщение metrics")
    window_s: float = Field(description="Окно, за которое считаются avg/min/max")
    servers: list[ServerMetrics]


def percent_stats(values: list[float]) -> tuple[float | None, float | None, float | None]:
    """avg, min, max списка процентов (None, если список пуст)."""
    if not values:
        return None, None, None
    return round(sum(values) / len(values), 2), round(min(values), 2), round(max(values), 2)


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
    """Тестовый сервер на 8 ГиБ RAM: CPU и память меняются плавно."""

    HOST = "demo.synex.local"
    PORT = 22
    MEMORY_TOTAL = 8 * GIB

    def __init__(
        self, seed: int | None = None, interval: float = 0.5, window: float = 30.0
    ) -> None:
        rng = random.Random(seed)
        self.interval = interval
        self.window = window
        self._cpu = _Walk(rng, center=rng.uniform(10, 40), low=0, high=100, step=8)
        self._memory = _Walk(rng, center=45, low=20, high=95, step=1.5)
        size = max(1, round(window / interval))
        self._cpu_history: deque[float] = deque(maxlen=size)
        self._memory_history: deque[float] = deque(maxlen=size)

    def sample(self) -> MetricsSnapshot:
        cpu = round(self._cpu.next(), 2)
        memory = round(self._memory.next(), 2)
        self._cpu_history.append(cpu)
        self._memory_history.append(memory)
        cpu_avg, cpu_min, cpu_max = percent_stats(list(self._cpu_history))
        mem_avg, mem_min, mem_max = percent_stats(list(self._memory_history))
        used = int(self.MEMORY_TOTAL * memory / 100)
        server = ServerMetrics(
            server=f"{self.HOST}:{self.PORT}",
            host=self.HOST,
            port=self.PORT,
            session_ids=[],
            status="ok",
            updated_at=time.time_ns() // 1_000_000,
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
