"""services/incident_response/workers/classifier.py — Incident Classifier Worker.

Deterministic — no LLM call. Classifies severity/recommended recovery
action per services/incident_response/utils.py's classify_incident(),
reading the incident_candidate fields Monitoring already computed
(component, severity, breach_cycles — see services/monitoring/models.py's
IncidentCandidate) plus a DevOps deployment-correlation lookup.
"""
from __future__ import annotations

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.incident_response.providers.devops_provider import DevOpsProvider
from services.incident_response.utils import classify_incident
from services.monitoring.models import AlertSeverity, MonitoredComponent


@AgentFactory.register("incident_classifier_worker")
class IncidentClassifierWorker(BaseAgent):
    """Deterministic — no LLM call. Only this worker decides the recovery action."""

    async def execute(self, task: TaskInput) -> AgentResult:
        arts = task.context.approved_artifacts
        component = MonitoredComponent(arts.get("__component__"))
        severity = AlertSeverity(arts.get("__severity__", "critical"))
        breach_cycles = int(arts.get("__breach_cycles__", 1))

        devops = DevOpsProvider(self._db_factory)
        correlation = await devops.recent_deployment_correlation()

        classification = classify_incident(
            component=component, severity=severity, breach_cycles=breach_cycles,
            recent_deployment_correlated=correlation is not None,
        )

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={
                "classification": classification.model_dump(mode="json"),
                "correlated_project_id": (correlation or {}).get("project_id"),
                "correlated_deployment_id": (correlation or {}).get("deployment_id"),
            },
            summary=f"Classified {component.value} incident: "
                    f"action={classification.recommended_action.value}",
            quality_score=1.0,
        )
