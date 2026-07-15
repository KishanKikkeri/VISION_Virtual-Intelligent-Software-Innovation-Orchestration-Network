"""services/incident_response/workers/notification.py — Notification Worker.

Deterministic — no LLM call. Notifies stakeholders of the incident's
current status via NotificationProvider (NATS) and
IncidentWebSocketProvider (real-time UI broadcast).
"""
from __future__ import annotations

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.incident_response.providers.notification_provider import NotificationProvider
from services.incident_response.providers.websocket_provider import IncidentWebSocketProvider


@AgentFactory.register("notification_worker")
class NotificationWorker(BaseAgent):
    """Deterministic — no LLM call."""

    async def execute(self, task: TaskInput) -> AgentResult:
        arts = task.context.approved_artifacts
        incident_id = arts.get("__incident_id__")
        component = arts.get("__component__")
        severity = arts.get("__severity__")
        classifier_output = arts.get("incident_classifier_worker", {})
        recovery_output = arts.get("recovery_worker", {})

        action = classifier_output.get("classification", {}).get("recommended_action", "none")
        recovery_status = recovery_output.get("recovery_status", "skipped")
        message = (f"Incident {incident_id} on {component} ({severity}): "
                   f"recovery action={action}, status={recovery_status}.")

        notifier = NotificationProvider(self._nats)
        event = await notifier.notify(incident_id, component, severity, message)

        ws = IncidentWebSocketProvider()
        await ws.broadcast("incident.timeline.updated", {
            "incident_id": incident_id, "message": message,
        })

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"notified": True, "message": message},
            summary="Stakeholders notified.", quality_score=1.0,
            nats_events=[event],
        )
