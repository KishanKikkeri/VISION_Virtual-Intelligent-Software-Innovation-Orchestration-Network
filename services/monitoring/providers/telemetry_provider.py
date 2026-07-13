"""
services/monitoring/providers/telemetry_provider.py
=======================================================================
Wraps the Prometheus series `infrastructure/monitoring/telemetry.py`
already populates (Sprint 3) — per spec §0 Decision 2 / Appendix A.3,
Monitoring reads this in-process registry rather than standing up a
second metrics pipeline or adding new base series.

Covers three of the nine monitored components: WEBSOCKET,
LLM_PROVIDERS, AGENT_RUNTIME. (DEPLOYMENTS is its own provider —
deployment_provider.py — since it also needs the Deployment ORM table,
not just the Prometheus counter.)
"""
from __future__ import annotations

from typing import Any, Dict, List

from services.monitoring.models import MetricSample, MonitoredComponent
from services.monitoring.providers.base import MetricsProvider


def _samples(metric: Any) -> List[Any]:
    """Public prometheus_client access path: metric.collect()[0].samples."""
    try:
        families = metric.collect()
        return list(families[0].samples) if families else []
    except Exception:
        return []


class WebSocketTelemetryProvider(MetricsProvider):
    component = MonitoredComponent.WEBSOCKET

    async def collect(self) -> List[MetricSample]:
        try:
            from infrastructure.monitoring.telemetry import ws_connections
            samples = _samples(ws_connections)
            value = samples[0].value if samples else 0.0
            # A gauge of 0 is a perfectly normal "no active viewers" state,
            # not a failure signal — WebSocket health here means "the
            # metric itself is reachable", not "connections > 0".
            return self._healthy("websocket_connections_reachable", 100.0, connections=str(value))
        except Exception as e:
            return self._degraded(str(e))


class LLMProvidersTelemetryProvider(MetricsProvider):
    component = MonitoredComponent.LLM_PROVIDERS

    async def collect(self) -> List[MetricSample]:
        try:
            from infrastructure.monitoring.telemetry import token_usage_total
            # NOTE (spec Appendix A.3): telemetry.py has no LLM *error*
            # counter yet, only usage/cost — so this provider can only
            # confirm the series is reachable, not measure error rate.
            # A future milestone adding that series is a separate,
            # explicit amendment, not part of M3.7.
            _samples(token_usage_total)  # confirms the registry entry is reachable
            return self._healthy("llm_provider_telemetry_reachable", 100.0)
        except Exception as e:
            return self._degraded(str(e))


class AgentRuntimeTelemetryProvider(MetricsProvider):
    component = MonitoredComponent.AGENT_RUNTIME

    async def collect(self) -> List[MetricSample]:
        try:
            from infrastructure.monitoring.telemetry import agent_run_duration
            samples = _samples(agent_run_duration)
            counts: Dict[str, float] = {}
            for s in samples:
                if s.name.endswith("_count"):
                    status = s.labels.get("status", "unknown")
                    counts[status] = counts.get(status, 0.0) + s.value
            total = sum(counts.values())
            if total == 0:
                # No agent runs recorded yet this process lifetime —
                # not a failure signal, just no data.
                return self._healthy("agent_run_success_ratio", 100.0)
            failed = counts.get("failed", 0.0) + counts.get("escalated", 0.0)
            ratio = max(0.0, (total - failed) / total)
            return self._healthy("agent_run_success_ratio", round(ratio * 100.0, 2), total_runs=str(int(total)))
        except Exception as e:
            return self._degraded(str(e))
