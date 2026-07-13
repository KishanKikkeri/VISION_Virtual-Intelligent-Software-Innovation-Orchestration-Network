"""
services/monitoring/providers/base.py — MetricsProvider interface.
=======================================================================
Per spec §0 Decision 2 / §6: every monitored system gets its own thin
provider implementing this Protocol. Providers for externally-owned
systems (Postgres, Qdrant, NATS, Docker) take a read-only client
reference; `TelemetryProvider` instead wraps the Prometheus registry
`infrastructure/monitoring/telemetry.py` already populates — it takes
no client at all.

Every provider MUST NOT raise — a failed collection degrades that
component's score to 0 for the cycle (spec §7 / §8: "a failed provider
... degrades that component's score to 0 rather than retrying"), it
never aborts the whole monitoring cycle.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Protocol

from services.monitoring.models import MetricSample, MonitoredComponent


class MetricsProvider(ABC):
    """Every concrete provider must implement collect()."""

    component: MonitoredComponent

    @abstractmethod
    async def collect(self) -> List[MetricSample]:
        """
        Collects one or more MetricSample rows for this provider's
        component. Must never raise — on internal failure, return a
        single MetricSample with value=0.0 (degrades the component to
        CRITICAL for this cycle rather than aborting collection).
        """
        raise NotImplementedError

    def _degraded(self, reason: str) -> List[MetricSample]:
        return [MetricSample(
            name=f"{self.component.value}_reachable",
            component=self.component, value=0.0, unit="score",
            labels={"reason": reason[:200]},
        )]

    def _healthy(self, name: str, value: float = 100.0, **labels: str) -> List[MetricSample]:
        return [MetricSample(
            name=name, component=self.component, value=value, unit="score", labels=labels,
        )]


class MetricsProviderProtocol(Protocol):
    """Structural alternative to the ABC above, for typing call sites
    that don't need to inherit MetricsProvider directly (e.g. test doubles)."""

    async def collect(self) -> List[MetricSample]: ...
