"""services/monitoring/providers/qdrant_provider.py — read-only Qdrant health.

Uses the same qdrant client BaseAgent already injects (`self._qdrant`
in core/runtime/base_agent.py) — no second Qdrant connection is opened.
"""
from __future__ import annotations

from typing import Any, List

from services.monitoring.models import MonitoredComponent
from services.monitoring.providers.base import MetricsProvider


class QdrantProvider(MetricsProvider):
    component = MonitoredComponent.QDRANT

    def __init__(self, qdrant_client: Any):
        self._qdrant = qdrant_client

    async def collect(self) -> List:
        if self._qdrant is None:
            return self._degraded("qdrant client not configured")
        try:
            # get_collections() is a lightweight read-only call available
            # on every qdrant-client version this platform pins.
            collections = self._qdrant.get_collections()
            count = len(getattr(collections, "collections", []) or [])
            return self._healthy("qdrant_collections_reachable", 100.0, collection_count=str(count))
        except Exception as e:
            return self._degraded(str(e))
