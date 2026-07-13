"""services/monitoring/workers/alert.py — Alert Worker.

Evaluates thresholds, dedupes, emits monitoring.alert/.warning (spec §1/§5).
Reads component scores computed earlier in the cycle from the sentinel
dunder key `__component_scores__` in approved_artifacts — the same
extra-context mechanism DevOps already uses for
`__exposed_port_override__` (services/devops/workers/dockerfile.py).
"""
from __future__ import annotations

from datetime import datetime

from core.contracts import AgentResult, NATSEvent, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.monitoring.context import evaluate_alerts
from services.monitoring.integration.monitoring_repository import (
    AlertHistoryRepository, AlertRepository,
)
from services.monitoring.models import MonitoredComponent


@AgentFactory.register("alert_worker")
class AlertWorker(BaseAgent):
    """Deterministic — no LLM call. Only this worker publishes monitoring.alert/.warning."""

    async def execute(self, task: TaskInput) -> AgentResult:
        raw_scores = task.context.approved_artifacts.get("__component_scores__", {})
        component_scores = {MonitoredComponent(k): v for k, v in raw_scores.items()}
        last_alert_at = dict(task.context.approved_artifacts.get("__last_alert_at__", {}))
        dedup_seconds = task.context.approved_artifacts.get("__dedup_window_seconds__", 300)

        events = evaluate_alerts(component_scores, last_alert_at, dedup_seconds)

        nats_events = []
        for event in events:
            try:
                async with self._db_factory() as db:
                    existing = await AlertRepository.find_open(db, event.component.value, event.severity.value)
                    if existing is None:
                        alert_row = await AlertRepository.open_alert(
                            db, event.component.value, event.severity.value, event.message)
                        await AlertHistoryRepository.record(db, alert_row.id, "raised")
            except Exception:
                pass

            subject = "monitoring.alert" if event.severity.value == "critical" else "monitoring.warning"
            nats_events.append(NATSEvent(subject=subject, payload={
                "component": event.component.value, "severity": event.severity.value,
                "message": event.message, "raised_at": event.raised_at.isoformat(),
            }))

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={
                "alerts_raised": [e.model_dump(mode="json") for e in events],
                "last_alert_at": {k: v.isoformat() for k, v in last_alert_at.items()},
            },
            summary=f"{len(events)} alert(s) raised this cycle",
            quality_score=1.0,
            nats_events=nats_events,
        )
