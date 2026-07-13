"""services/monitoring/providers/nats_provider.py — read-only NATS/JetStream health.

Uses the shared NATSClient (infrastructure/messaging/nats_client.py)
already connected by every service's lifespan — no second connection.
"""
from __future__ import annotations

from typing import Any, List

from services.monitoring.models import MonitoredComponent
from services.monitoring.providers.base import MetricsProvider


class NatsProvider(MetricsProvider):
    component = MonitoredComponent.NATS

    def __init__(self, nats_client: Any):
        self._nats = nats_client

    async def collect(self) -> List:
        if self._nats is None or getattr(self._nats, "_nc", None) is None:
            return self._degraded("nats client not connected")
        try:
            nc = self._nats._nc
            connected = not nc.is_closed if hasattr(nc, "is_closed") else True
            if not connected:
                return self._degraded("nats connection closed")
            return self._healthy("nats_connectivity", 100.0)
        except Exception as e:
            return self._degraded(str(e))
