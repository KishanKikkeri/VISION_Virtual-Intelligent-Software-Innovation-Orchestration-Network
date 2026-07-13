"""services/devops/workers/provisioner.py — Provisioner Worker (Deployment Worker)."""
from __future__ import annotations

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.devops.integration.deployment_repository import DeploymentHistoryRepository, DeploymentRepository
from services.devops.providers import default_provider


@AgentFactory.register("provisioner_worker")
class ProvisionerWorker(BaseAgent):
    """
    Executes the actual deployment via a DeploymentProvider (see
    providers/base.py). This is the spec's "Deployment Worker" —
    registered under infrastructure_ops_lead per AGENT_REGISTRY (see
    docs/M3.6 handover Department Structure note).
    """

    async def execute(self, task: TaskInput) -> AgentResult:
        plan = task.context.get_artifact("deployment_plan", {})
        provider = task.context.approved_artifacts.get("__provider__") or default_provider()
        deployment_id = task.context.approved_artifacts.get("__deployment_id__")

        result = await provider.deploy(task.project_id, plan)
        success = bool(result.get("success"))

        if deployment_id:
            try:
                async with self._db_factory() as db:
                    await DeploymentRepository.update_status(
                        db, deployment_id, "deploying" if success else "failed",
                        failure_reason=None if success else result.get("detail"))
                    await DeploymentHistoryRepository.record(
                        db, deployment_id, task.project_id, "deployment.attempted",
                        "deploying" if success else "failed", payload=result)
            except Exception:
                pass   # DB durability is best-effort in this sandbox; provider result is authoritative

        status = TaskStatus.COMPLETED if success else TaskStatus.FAILED
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=status,
            content={"success": success, "deployment_ref": result.get("deployment_ref", ""),
                     "detail": result.get("detail", "")},
            summary=f"Deployment {'succeeded' if success else 'failed'}: {result.get('detail', '')}",
            quality_score=0.9 if success else 0.0,
            failure_reason=None if success else result.get("detail", "deployment failed"),
        )
