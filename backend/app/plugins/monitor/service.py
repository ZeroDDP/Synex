"""Мониторинг серверов: опрос synex_worker, агрегация, публикация в /ws.

* Раз в MONITOR_POLL_INTERVAL каждый сервер (host:port) с открытыми SSH-сессиями
  опрашивается действием monitor_get_values через одну из своих сессий. Опросы разных
  серверов идут параллельно: у каждой сессии своё подключение к воркеру.
* Опрос берёт только свободную сессию (SSHSessionManager.request_if_idle): воркер
  выполняет запросы подключения по очереди, и опрос не должен вставать за долгой
  командой. Если все сессии сервера заняты, замер пропускается (busy), а через
  MONITOR_STALE_AFTER без свежих данных сервер получает статус stale.
* Замеры хранятся за окно MONITOR_WINDOW: из них считаются avg/min/max.
* Раз в MONITOR_PUBLISH_INTERVAL (500 мс) сводка MetricsSnapshot всех серверов уходит
  в событие metrics, а хаб /ws рассылает её UI.
* Серверы опрашиваются, только пока метрики кому-то нужны: к /ws подключён UI или
  недавно был GET /api/monitor/metrics.
* Сеть и раздел "/" приходят в том же ответе monitor_get_values (воркер с 2026-11-17),
  то есть с каждым замером.
* Все диски и видеокарты (hardware.py) обновляются реже, раз в MONITOR_HARDWARE_INTERVAL:
  после удачного замера CPU и памяти, через ту же свободную сессию.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from collections.abc import Callable
from typing import Any

import structlog

from app.core.config import settings
from app.core.errors import AppError, WorkerTimeoutError
from app.core.event_bus import event_bus
from app.core.periodic import Periodic
from app.plugins.monitor.aggregator import Sample, ServerState, server_key
from app.plugins.monitor.hardware import COMMAND as HARDWARE_COMMAND
from app.plugins.monitor.hardware import parse_hardware
from app.plugins.monitor.values import MonitorValuesError, parse_values
from app.plugins.ssh.errors import SSHNotConnectedError, SSHSessionNotFoundError
from app.plugins.ssh.manager import SSH_SESSION_EVENT, SSHSessionManager, ssh_manager
from app.plugins.ssh.session import SSHSession
from app.ws import ws_hub
from app.ws.metrics import METRICS_EVENT, MetricsSnapshot

logger = structlog.get_logger()

MONITOR_ACTION = "monitor_get_values"


class MetricsMonitor:
    def __init__(
        self,
        sessions: SSHSessionManager,
        demand: Callable[[], bool] | None = None,
    ) -> None:
        self.sessions = sessions
        self._demand = demand or self._has_viewers
        self._servers: dict[str, ServerState] = {}
        self._poller: Periodic | None = None
        self._publisher: Periodic | None = None
        self._polls: set[asyncio.Task[Any]] = set()
        self._last_read = -math.inf

    # ---------------------------------------------------------- lifecycle

    @property
    def running(self) -> bool:
        return self._poller is not None and self._poller.running

    def start(self, *, publish: bool = True) -> None:
        """publish=False: только опрос (для GET /api/monitor/metrics), без /ws."""
        if self.running:
            return
        self._poller = Periodic("monitor-poll", settings.MONITOR_POLL_INTERVAL, self.poll)
        self._poller.start()
        if publish:
            # Отправка сдвинута на полпериода опроса: замер (~50 мс) успевает попасть в
            # ближайшее сообщение, и данные в нём не старше половины периода.
            self._publisher = Periodic(
                "monitor-publish",
                settings.MONITOR_PUBLISH_INTERVAL,
                self.publish,
                delay=min(settings.MONITOR_POLL_INTERVAL, settings.MONITOR_PUBLISH_INTERVAL) / 2,
            )
            self._publisher.start()
        event_bus.subscribe(SSH_SESSION_EVENT, self._on_session_event)
        logger.info(
            "Server monitoring started",
            poll_interval=settings.MONITOR_POLL_INTERVAL,
            publish_interval=settings.MONITOR_PUBLISH_INTERVAL if publish else None,
        )

    async def stop(self) -> None:
        event_bus.unsubscribe(SSH_SESSION_EVENT, self._on_session_event)
        for periodic in (self._poller, self._publisher):
            if periodic is not None:
                await periodic.stop()
        self._poller = self._publisher = None
        polls, self._polls = list(self._polls), set()
        for task in polls:
            task.cancel()
        for task in polls:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._servers.clear()

    def touch(self) -> None:
        """Метрики кто-то читает через REST: опрашивать ещё MONITOR_DEMAND_TIMEOUT сек."""
        self._last_read = time.monotonic()

    def _has_viewers(self) -> bool:
        recent = time.monotonic() - self._last_read < settings.MONITOR_DEMAND_TIMEOUT
        return ws_hub.client_count > 0 or recent

    # ------------------------------------------------------------ polling

    async def poll(self) -> list[asyncio.Task[Any]]:
        """Запустить опрос каждого сервера, который сейчас не опрашивается.

        Не ждёт ответов: долгий опрос одного сервера не задерживает тики. Возвращает
        запущенные опросы (их можно дождаться).
        """
        groups = self._sync()
        if not self._demand():
            return []
        started = []
        for key, sessions in groups.items():
            state = self._servers[key]
            if not state.polling:
                state.polling = True
                started.append(self._spawn(self._poll_server(state, sessions)))
        return started

    async def _poll_server(self, state: ServerState, sessions: list[SSHSession]) -> None:
        try:
            ordered = sorted(sessions, key=lambda s: s.id != state.preferred_session)
            for session in ordered:
                try:
                    response = await self.sessions.request_if_idle(
                        session.id, MONITOR_ACTION, timeout=settings.MONITOR_POLL_TIMEOUT
                    )
                except (SSHSessionNotFoundError, SSHNotConnectedError):
                    state.forget_session(session.id)
                    continue  # сессию закрыли, пробуем следующую
                except WorkerTimeoutError:
                    state.record_error(
                        f"No metrics from the worker within {settings.MONITOR_POLL_TIMEOUT:g}s"
                    )
                    return
                except AppError as e:
                    state.record_error(e.detail)
                    return
                if response is None:
                    continue  # сессия занята командой

                try:
                    values = parse_values(response)
                except MonitorValuesError as e:
                    state.record_error(str(e))
                    return
                first = session.id not in state.primed
                state.primed.add(session.id)
                state.preferred_session = session.id
                state.record(
                    Sample(
                        at=time.monotonic(),
                        wall_ms=time.time_ns() // 1_000_000,
                        cpu=None if first else values.cpu_percent,
                        values=values,
                    )
                )
                if state.hardware_due(time.monotonic(), settings.MONITOR_HARDWARE_INTERVAL):
                    await self._poll_hardware(state, session)
                return
            state.record_busy()
        except Exception as e:
            logger.exception("Server monitoring poll failed", server=state.key)
            state.record_error(repr(e))
        finally:
            state.polling = False

    async def _poll_hardware(self, state: ServerState, session: SSHSession) -> None:
        """Диски и GPU одной командой. Неудача не мешает CPU и памяти: следующая попытка
        через MONITOR_HARDWARE_INTERVAL."""
        started, wall_ms = time.monotonic(), time.time_ns() // 1_000_000
        try:
            response = await self.sessions.request_if_idle(
                session.id,
                "ssh_exec",
                {"command": HARDWARE_COMMAND},
                timeout=settings.MONITOR_POLL_TIMEOUT,
            )
        except AppError as e:
            logger.debug("Disk and GPU poll failed", server=state.key, error=e.detail)
            state.hardware_at = started
            return
        if response is None:
            return  # сессию успели занять: попробуем на следующем тике
        state.record_hardware(parse_hardware(str(response.get("stdout") or "")), started, wall_ms)

    def _sync(self) -> dict[str, list[SSHSession]]:
        """Группирует открытые сессии по серверам и забывает серверы без сессий."""
        groups: dict[str, list[SSHSession]] = {}
        for session in self.sessions.sessions():
            groups.setdefault(server_key(session.host, session.port), []).append(session)
        for key in list(self._servers):
            if key not in groups:
                del self._servers[key]
        for key, sessions in groups.items():
            state = self._servers.get(key)
            if state is None:
                first = sessions[0]
                state = self._servers[key] = ServerState(
                    first.host, first.port, settings.MONITOR_WINDOW
                )
            alive = {s.id for s in sessions}
            for session_id in list(state.primed):
                if session_id not in alive:
                    state.forget_session(session_id)
        return groups

    async def _on_session_event(self, event: dict[str, Any]) -> None:
        # Новый сервер: первый замер сразу, не дожидаясь тика
        if event.get("action") == "opened" and self.running:
            await self.poll()

    def _spawn(self, awaitable: Any) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(awaitable)
        self._polls.add(task)
        task.add_done_callback(self._polls.discard)
        return task

    # --------------------------------------------------------- publishing

    def snapshot(self) -> MetricsSnapshot:
        groups = self._sync()
        now, now_ms = time.monotonic(), time.time_ns() // 1_000_000
        servers = [
            self._servers[key].metrics(
                [s.id for s in groups[key]], settings.MONITOR_STALE_AFTER, now, now_ms
            )
            for key in sorted(groups)
        ]
        return MetricsSnapshot(
            source="ssh",
            interval_ms=round(settings.MONITOR_PUBLISH_INTERVAL * 1000),
            window_s=settings.MONITOR_WINDOW,
            servers=servers,
        )

    async def publish(self) -> None:
        await event_bus.publish(METRICS_EVENT, self.snapshot())


metrics_monitor = MetricsMonitor(ssh_manager)
