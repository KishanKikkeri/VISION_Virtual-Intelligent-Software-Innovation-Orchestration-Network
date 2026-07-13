"""
services/monitoring/head — L3 MonitoringHead: owns one full cycle
end-to-end (spec §1: "Own the continuous monitoring cycle end-to-end;
publish health score").

Implementation note on the conceptual 8-step diagram in
docs/M3.7_Monitoring_Service_Specification_v1.md §8
(collect_metrics -> aggregate -> analyze -> health_score -> dashboard/
alert -> incident? -> publish): those are conceptual stages, not a
1:1 map to LangGraph nodes. Mirroring how DevOpsHead's two stages
(services/devops/head/__init__.py) each internally run multiple leads
before returning, MonitoringHead's `score_and_publish` task_type
performs aggregate + analyze + health_score + incident-decision + publish
in one call, after MetricsLead/ObservabilityLead (collect) and before
AlertingLead (dashboard/alert) — see services/monitoring/workflows/
monitoring_graph.py for the exact node wiring and the deviation note
repeated there.
"""
from __future__ import annotations

from typing import Any, Dict, List

import structlog

from core.contracts import AgentResult, NATSEvent, TaskStatus, WebSocketEvent
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.monitoring.context import (
    build_capacity_forecast,
    build_health_report,
    decide_incident,
)
from services.monitoring.integration.monitoring_repository import (
    CapacityForecastRepository, MetricSampleRepository, MetricRepository, SystemHealthRepository,
)
from services.monitoring.models import MonitoredComponent

log = structlog.get_logger(__name__)


@AgentFactory.register("monitoring_head")
class MonitoringHead(BaseAgent):
    """L3 — Sole orchestrator of one Monitoring cycle."""

    async def execute(self, task: TaskInput) -> AgentResult:
        raw_scores = task.context.approved_artifacts.get("__component_scores__", {})
        component_scores = {MonitoredComponent(k): v for k, v in raw_scores.items()}

        health_report = build_health_report(component_scores)
        task.context.approved_artifacts["__health_report__"] = health_report.model_dump(mode="json")

        # Capacity forecast — one per component, using trailing samples
        # already persisted this cycle (and prior cycles) by the metrics workers.
        forecasts = await self._build_forecasts(component_scores)

        # Incident escalation — consecutive_critical_count persists across
        # cycles via the LangGraph checkpointer's stable thread_id (spec §8);
        # the graph passes it in and reads the mutated copy back out.
        consecutive_critical_count = dict(
            task.context.approved_artifacts.get("__consecutive_critical_count__", {}))
        breach_cycles_required = task.context.approved_artifacts.get(
            "__incident_breach_cycles__", 3)
        incidents = decide_incident(component_scores, consecutive_critical_count, breach_cycles_required)
        task.context.approved_artifacts["__consecutive_critical_count__"] = consecutive_critical_count

        artifacts: List[Dict[str, Any]] = []

        health_artifact = await self.create_artifact(task, "system_health_report", health_report.model_dump(mode="json"))
        artifacts.append(health_artifact)

        snapshot_samples = task.context.approved_artifacts.get("__metric_samples__", [])
        snapshot_artifact = await self.create_artifact(
            task, "metrics_snapshot", {"samples": snapshot_samples})
        artifacts.append(snapshot_artifact)

        for forecast in forecasts:
            forecast_artifact = await self.create_artifact(task, "capacity_forecast", forecast.model_dump(mode="json"))
            artifacts.append(forecast_artifact)

        try:
            async with self._db_factory() as db:
                await SystemHealthRepository.record(
                    db, health_report.health_score, health_report.status.value,
                    health_report.component_scores)
        except Exception:
            pass

        incident_artifacts = []
        for incident in incidents:
            incident_artifact = await self.create_artifact(task, "incident_candidate", incident.model_dump(mode="json"))
            incident_artifacts.append(incident_artifact)
        artifacts.extend(incident_artifacts)

        nats_events = [
            NATSEvent(subject="monitoring.metrics.updated", payload={
                "health_score": health_report.health_score, "status": health_report.status.value,
            }),
        ]
        for incident in incidents:
            nats_events.append(NATSEvent(subject="monitoring.incident", payload={
                "component": incident.component.value, "severity": incident.severity.value,
                "breach_cycles": incident.breach_cycles, "incident_id": incident.incident_id,
            }))

        # Platform-wide, not project-scoped — this is the first consumer
        # of broadcast_system (spec §0 Decision 1 reconnaissance / Appendix A.2).
        try:
            from infrastructure.websocket.manager import ws_manager
            await ws_manager.broadcast_system("monitoring.metrics.updated", {
                "health_score": health_report.health_score, "status": health_report.status.value,
            })
        except Exception as e:
            log.warning("monitoring_broadcast_system_failed", error=str(e))

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={
                "health_score": health_report.health_score, "status": health_report.status.value,
                "component_scores": health_report.component_scores,
                "incidents": [i.model_dump(mode="json") for i in incidents],
                "consecutive_critical_count": consecutive_critical_count,
            },
            summary=f"Cycle scored: health_score={health_report.health_score} "
                    f"({health_report.status.value}), {len(incidents)} incident(s)",
            quality_score=1.0, artifacts=artifacts, nats_events=nats_events,
        )

    async def _build_forecasts(self, component_scores):
        from services.monitoring.models import CapacityForecast as CapacityForecastModel

        forecasts = []
        cycle_interval = 30  # settings.monitoring_cycle_interval_seconds, injected by caller in prod
        for component, score in component_scores.items():
            try:
                async with self._db_factory() as db:
                    metric = await MetricRepository.get_or_create(
                        db, f"{component.value}_reachable", component.value)
                    trailing = await MetricSampleRepository.trailing_values(db, metric.id, limit=20)
                forecast = build_capacity_forecast(component, trailing or [score], cycle_interval)
                async with self._db_factory() as db:
                    await CapacityForecastRepository.record(
                        db, component.value, forecast.trend_slope, forecast.projected_breach_at)
                forecasts.append(forecast)
            except Exception:
                forecasts.append(CapacityForecastModel(component=component, trend_slope=0.0))
        return forecasts

    async def publish_phase_completed(self, task: TaskInput, health_score: float, status: str) -> None:
        """Called once per cycle by the graph's final publish node."""
        await self.publish_event("monitoring.phase.completed", {
            "health_score": health_score, "status": status,
        })
