from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

logger = structlog.get_logger()

Callback = Callable[[Any], Awaitable[None] | None]


class EventBus:
    """Внутренняя шина событий между ядром и плагинами.

    Синхронные обработчики выполняются в пуле потоков, асинхронные как задачи.
    Ошибка одного обработчика не мешает остальным.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callback]] = defaultdict(list)

    def subscribe(self, event_type: str, callback: Callback) -> None:
        self._subscribers[event_type].append(callback)

    def unsubscribe(self, event_type: str, callback: Callback) -> None:
        try:
            self._subscribers[event_type].remove(callback)
        except ValueError:
            pass

    async def publish(self, event_type: str, data: Any) -> None:
        callbacks = list(self._subscribers.get(event_type, ()))
        if not callbacks:
            return

        tasks = []
        for callback in callbacks:
            if inspect.iscoroutinefunction(callback):
                tasks.append(asyncio.create_task(callback(data)))
            else:
                tasks.append(asyncio.create_task(asyncio.to_thread(callback, data)))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for callback, result in zip(callbacks, results, strict=True):
            if isinstance(result, Exception):
                logger.error(
                    "EventBus callback error",
                    event_type=event_type,
                    callback=getattr(callback, "__qualname__", repr(callback)),
                    error=repr(result),
                )


event_bus = EventBus()
