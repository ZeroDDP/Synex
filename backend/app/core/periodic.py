from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

import structlog

logger = structlog.get_logger()


class Periodic:
    """Фоновая задача: вызывает func раз в interval секунд.

    Тики считаются от старта, поэтому период не накапливает время работы func. Если
    loop был надолго занят, пропущенные тики не догоняются. Ошибка func пишется в лог
    и не останавливает задачу. delay: сдвиг первого тика, сек.
    """

    def __init__(
        self,
        name: str,
        interval: float,
        func: Callable[[], Awaitable[object]],
        delay: float = 0.0,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive")
        self.name = name
        self.interval = interval
        self.delay = max(0.0, delay)
        self._func = func
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._run(), name=self.name)

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        if self.delay:
            await asyncio.sleep(self.delay)
        next_tick = loop.time()
        while True:
            try:
                await self._func()
            except Exception as e:
                logger.error("Periodic task failed", task=self.name, error=repr(e))
            next_tick = max(next_tick + self.interval, loop.time())
            await asyncio.sleep(next_tick - loop.time())
