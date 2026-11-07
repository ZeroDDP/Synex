from __future__ import annotations

import asyncio
import weakref
from types import TracebackType


class LoopLock:
    """asyncio.Lock для объекта, который живёт дольше одного event loop.

    Обычный asyncio.Lock привязывается к первому loop, где его пришлось ждать, и в
    другом loop падает с RuntimeError. Синглтоны backend переживают несколько loop
    (например, в тестах), поэтому для каждого loop здесь свой Lock.
    """

    def __init__(self) -> None:
        self._locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
            weakref.WeakKeyDictionary()
        )

    def _current(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._locks.get(loop)
        if lock is None:
            lock = self._locks[loop] = asyncio.Lock()
        return lock

    def locked(self) -> bool:
        return self._current().locked()

    async def acquire(self, timeout: float | None = None) -> bool:
        """Захватить; False, если не удалось за timeout секунд."""
        try:
            async with asyncio.timeout(timeout):
                await self._current().acquire()
        except TimeoutError:
            return False
        return True

    def release(self) -> None:
        self._current().release()

    async def __aenter__(self) -> None:
        await self._current().acquire()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._current().release()
