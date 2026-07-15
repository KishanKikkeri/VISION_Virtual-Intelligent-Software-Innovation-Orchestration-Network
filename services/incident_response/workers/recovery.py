"""services/incident_response/workers/recovery.py — Recovery Worker.

Deterministic — no LLM call. Verifies rollback_worker's outcome (when
action==ROLLBACK) or records the non-rollback recovery step taken
(RESTART/MANUAL/NONE), and is the only worker that creates the
`recovery_plan` artifact (spec §12).

Known limitation (documented in docs/M3.8_Incident_Response_Handover.md
"known pre-existing issues"): the platform has no automated
restart/remediation execution API for RESTART-classified incidents —
DevOps only exposes rollback + health-check endpoints. RESTART/MANUAL
therefore produce an actionable recovery_plan for a human operator
rather than an automated action, exactly like Monitoring's alert_worker
surfaces a problem without auto-remediating it.
"""
from __future__ import annotations

from datetime import datetime

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.incident_response.models import (
    RecoveryActionStatus, RecoveryActionType, RecoveryPlan,
)
from services.monitoring.models import MonitoredComponent

_MANUAL_STEPS = {
    RecoveryActionType.RESTART: [
        "Page the on-call operator.",
        "Restart the affected service/container.",
        "Confirm the component's next Monitoring cycle reports HEALTHY.",
    ],
    RecoveryActionType.MANUAL: [
        "Review the incident's evidence and timeline.",
        "Decide on a remediation with the on-call operator.",
    ],
    RecoveryActionType.NONE: [],
}


@AgentFactory.register("recovery_worker")
class RecoveryWorker(BaseAgent):
    """Deterministic — no LLM call. Only this worker creates `recovery_plan`."""

    async def execute(self, task: TaskInput) -> AgentResult:
        arts = task.context.approved_artifacts
        incident_id = arts.get("__incident_id__")
        component = MonitoredComponent(arts.get("__component__"))
        classifier_output = arts.get("incident_classifier_worker", {})
        action = RecoveryActionType(classifier_output.get("classification", {}).get("recommended_action", "none"))
        rollback_result = arts.get("rollback_worker", {})

        if action == RecoveryActionType.ROLLBACK:
            status = RecoveryActionStatus.COMPLETED if rollback_result.get("status") == "completed" \
                else RecoveryActionStatus.FAILED
            steps = [f"DevOps rollback triggered for the correlated deployment.",
                     f"Outcome: {rollback_result.get('status', 'unknown')}."]
        elif action == RecoveryActionType.NONE:
            status = RecoveryActionStatus.SKIPPED
            steps = []
        else:
            status = RecoveryActionStatus.SKIPPED  # requires a human — not auto-executed
            steps = _MANUAL_STEPS.get(action, [])

        plan = RecoveryPlan(
            incident_id=incident_id, action_type=action, component=component,
            steps=steps, status=status,
            triggered_at=datetime.utcnow() if action != RecoveryActionType.NONE else None,
            completed_at=datetime.utcnow() if status == RecoveryActionStatus.COMPLETED else None,
            detail=rollback_result if action == RecoveryActionType.ROLLBACK else {},
        )
        artifact = await self.create_artifact(task, "recovery_plan", plan.model_dump(mode="json"))

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"recovery_status": status.value, "action_type": action.value,
                     "recovery_plan": plan.model_dump(mode="json")},
            summary=f"Recovery plan: action={action.value}, status={status.value}",
            quality_score=1.0, artifacts=[artifact],
        )
