"""services/incident_response/workers/rollback.py — Rollback Worker.

Deterministic — no LLM call. The ONLY worker permitted to call
DevOpsProvider.trigger_rollback() (mirrors Engineering's commit_worker
being "the only Engineering worker permitted to call Repository
Service" — same separation-of-duties pattern, see core/runtime/
factory.py's AGENT_REGISTRY responsibilities for commit_worker).
Only acts when the classifier recommended RecoveryActionType.ROLLBACK.
"""
from __future__ import annotations

from core.contracts import AgentResult, NATSEvent, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.incident_response.integration.incident_repository import RecoveryActionRepository
from services.incident_response.models import RecoveryActionType
from services.incident_response.providers.devops_provider import DevOpsProvider


@AgentFactory.register("rollback_worker")
class RollbackWorker(BaseAgent):
    """Deterministic — no LLM call."""

    async def execute(self, task: TaskInput) -> AgentResult:
        arts = task.context.approved_artifacts
        incident_id = arts.get("__incident_id__")
        classifier_output = arts.get("incident_classifier_worker", {})
        action = classifier_output.get("classification", {}).get("recommended_action", "none")
        project_id = classifier_output.get("correlated_project_id")

        if action != RecoveryActionType.ROLLBACK.value or not project_id:
            return AgentResult(
                task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
                content={"action_type": action, "status": "skipped"},
                summary="No rollback required for this incident.", quality_score=1.0,
            )

        try:
            async with self._db_factory() as db:
                action_row = await RecoveryActionRepository.create(
                    db, incident_id, RecoveryActionType.ROLLBACK.value, project_id=project_id)
                await RecoveryActionRepository.update_status(db, action_row.id, "in_progress")
        except Exception:
            action_row = None

        devops = DevOpsProvider(self._db_factory)
        result = await devops.trigger_rollback(
            project_id, reason=f"incident_response auto-rollback for incident {incident_id}")

        final_status = "completed" if result.get("status") not in (None, "unreachable") else "failed"
        if action_row is not None:
            try:
                async with self._db_factory() as db:
                    await RecoveryActionRepository.update_status(db, action_row.id, final_status, result)
            except Exception:
                pass

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"action_type": "rollback", "status": final_status, "devops_response": result},
            summary=f"Rollback {final_status} for project {project_id}",
            quality_score=1.0 if final_status == "completed" else 0.5,
            nats_events=[NATSEvent(subject="incident.rollback.requested", payload={
                "incident_id": incident_id, "project_id": project_id, "status": final_status,
            })],
        )
