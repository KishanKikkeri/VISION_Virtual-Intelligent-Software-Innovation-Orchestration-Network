"""services/monitoring/providers/postgres_provider.py — read-only Postgres health.

Per spec §6, Monitoring never writes to another department's tables;
this provider issues a single `SELECT 1` (connectivity + pool checkout
latency) — no schema introspection, no writes.
"""
from __future__ import annotations

import time
from typing import Any, List

from sqlalchemy import text

from services.monitoring.models import MetricSample, MonitoredComponent
from services.monitoring.providers.base import MetricsProvider

# A checkout+roundtrip slower than this is treated as degraded, not just latency noise.
SLOW_QUERY_THRESHOLD_MS = 500


class PostgresProvider(MetricsProvider):
    component = MonitoredComponent.POSTGRES

    def __init__(self, db_factory: Any):
        self._db_factory = db_factory

    async def collect(self) -> List[MetricSample]:
        if self._db_factory is None:
            return self._degraded("db_factory not configured")
        try:
            t0 = time.monotonic()
            async with self._db_factory() as db:
                await db.execute(text("SELECT 1"))
            latency_ms = (time.monotonic() - t0) * 1000
            score = 100.0 if latency_ms < SLOW_QUERY_THRESHOLD_MS else 60.0
            return self._healthy("postgres_connectivity", score, latency_ms=str(round(latency_ms, 2)))
        except Exception as e:
            return self._degraded(str(e))
