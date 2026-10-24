"""Формат метрик для UI и их тестовый источник.

Пока настоящего мониторинга нет, FakeMetricsPublisher раз в WS_FAKE_METRICS_INTERVAL
секунд (по умолчанию 500 мс) публикует в шину событий правдоподобные данные, а хаб /ws
пересылает событие METRICS_EVENT всем клиентам. Настоящий сборщик метрик будет
публиковать тот же Metrics в то же событие, и фронтенду ничего менять не придётся.

Единицы: размеры в байтах, скорости в байтах в секунду, проценты 0–100, температура °C.
"""

from __future__ import annotations

import asyncio
import contextlib
import random

import structlog
from pydantic import BaseModel

from app.core.event_bus import event_bus

logger = structlog.get_logger()

METRICS_EVENT = "metrics"

MIB = 1024**2
GIB = 1024**3


class CpuMetrics(BaseModel):
    percent: float  # средняя загрузка всех ядер
    per_core: list[float]


class MemoryMetrics(BaseModel):
    total: int
    used: int
    percent: float


class GpuMetrics(BaseModel):
    name: str
    percent: float
    memory_total: int
    memory_used: int
    temperature: float


class DiskMetrics(BaseModel):
    mount: str
    total: int
    used: int
    percent: float
    read_bytes_per_sec: int
    write_bytes_per_sec: int


class Metrics(BaseModel):
    source: str  # "fake" у тестовых данных
    cpu: CpuMetrics
    memory: MemoryMetrics
    gpus: list[GpuMetrics]
    disks: list[DiskMetrics]


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
    """Тестовая машина: 8 ядер, 32 ГиБ RAM, одна GPU на 12 ГиБ и два диска."""

    CORES = 8
    MEMORY_TOTAL = 32 * GIB
    GPU_NAME = "Synex Fake GPU"
    GPU_MEMORY_TOTAL = 12 * GIB
    DISKS = (("/", 512 * GIB, 41.0), ("/data", 2048 * GIB, 68.0))  # точка, размер, занято %

    def __init__(self, seed: int | None = None) -> None:
        rng = random.Random(seed)
        self._cores = [
            _Walk(rng, center=rng.uniform(10, 40), low=0, high=100, step=8)
            for _ in range(self.CORES)
        ]
        self._memory = _Walk(rng, center=45, low=20, high=95, step=1.5)
        self._gpu = _Walk(rng, center=30, low=0, high=100, step=10)
        self._gpu_memory = _Walk(rng, center=35, low=5, high=95, step=2)
        self._gpu_temperature = _Walk(rng, center=50, low=30, high=90, step=0.8)
        self._disks = [
            (
                mount,
                total,
                _Walk(rng, center=used, low=used - 2, high=used + 2, step=0.02),
                _Walk(rng, center=20, low=0, high=400, step=15),  # чтение, МиБ/с
                _Walk(rng, center=8, low=0, high=250, step=10),  # запись, МиБ/с
            )
            for mount, total, used in self.DISKS
        ]

    def sample(self) -> Metrics:
        cores = [round(core.next(), 1) for core in self._cores]
        memory = self._memory.next()
        gpu = self._gpu.next()
        # Под нагрузкой GPU греется: температура тянется к значению, зависящему от загрузки
        self._gpu_temperature.center = 38 + gpu * 0.45
        gpu_memory = self._gpu_memory.next()

        disks = []
        for mount, total, used, read, write in self._disks:
            disk_percent = used.next()
            disks.append(
                DiskMetrics(
                    mount=mount,
                    total=total,
                    used=int(total * disk_percent / 100),
                    percent=round(disk_percent, 1),
                    read_bytes_per_sec=int(read.next() * MIB),
                    write_bytes_per_sec=int(write.next() * MIB),
                )
            )

        return Metrics(
            source="fake",
            cpu=CpuMetrics(percent=round(sum(cores) / len(cores), 1), per_core=cores),
            memory=MemoryMetrics(
                total=self.MEMORY_TOTAL,
                used=int(self.MEMORY_TOTAL * memory / 100),
                percent=round(memory, 1),
            ),
            gpus=[
                GpuMetrics(
                    name=self.GPU_NAME,
                    percent=round(gpu, 1),
                    memory_total=self.GPU_MEMORY_TOTAL,
                    memory_used=int(self.GPU_MEMORY_TOTAL * gpu_memory / 100),
                    temperature=round(self._gpu_temperature.next(), 1),
                )
            ],
            disks=disks,
        )


class FakeMetricsPublisher:
    """Фоновая задача: раз в interval секунд публикует тестовые метрики в METRICS_EVENT."""

    def __init__(self, interval: float, source: FakeMetricsSource | None = None) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive")
        self.interval = interval
        self._source = source or FakeMetricsSource()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._run(), name="fake-metrics")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        while True:
            try:
                await event_bus.publish(METRICS_EVENT, self._source.sample())
            except Exception as e:
                logger.error("Failed to publish fake metrics", error=repr(e))
            # Тики считаются от старта, поэтому период не накапливает задержку
            # публикации. Если loop был надолго занят, пропущенные тики не догоняются.
            next_tick = max(next_tick + self.interval, loop.time())
            await asyncio.sleep(next_tick - loop.time())
