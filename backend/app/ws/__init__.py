"""Поток данных из backend в UI по WebSocket (/ws).

hub.py: соединения, протокол и рассылка; metrics.py: формат метрик и их тестовый
источник; routes.py: сам эндпоинт.
"""

from app.ws.hub import WebSocketHub, ws_hub
from app.ws.metrics import METRICS_EVENT, FakeMetricsPublisher, Metrics
from app.ws.routes import router

__all__ = [
    "METRICS_EVENT",
    "FakeMetricsPublisher",
    "Metrics",
    "WebSocketHub",
    "router",
    "ws_hub",
]
