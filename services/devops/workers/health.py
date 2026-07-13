"""services/devops/workers/health.py — Health Check Worker."""
from __future__ import annotations

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.devops.integration.deployment_repository import DeploymentHealthRepository
from services.devops.providers import default_provider


@AgentFactory.register("health_check_worker")
class HealthCheckWorker(BaseAgent):
    """
    Runs the spec's required post-deploy health checks (service
    reachable, REST health endpoint, database connected, NATS
    connected, WebSocket connected, startup successful) against the
    just-provisioned deployment.
    """

    async def execute(self, task: TaskInput) -> AgentResult:
        provider = task.context.approved_artifacts.get("__provider__") or default_provider()
        deployment_id = task.context.approved_artifacts.get("__deployment_id__")
        provisioner_result = task.context.approved_artifacts.get("provisioner_worker", {})
        deployment_ref = provisioner_result.get("deployment_ref", "")

        result = await provider.health_check(task.project_id, deployment_ref)
        checks = result.get("checks", [])
        all_passed = bool(checks) and all(c.get("passed") for c in checks)

        if deployment_id:
            try:
                async with self._db_factory() as db:
                    await DeploymentHealthRepository.record_checks(db, deployment_id, task.project_id, checks)
            except Exception:
                pass

        artifact = await self.create_artifact(
            task, "health_report",
            {"project_id": task.project_id, "deployment_id": deployment_id, "checks": checks},
        )
        status = TaskStatus.COMPLETED if all_passed else TaskStatus.FAILED
        failed_names = [c["check_name"] for c in checks if not c.get("passed")]
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=status,
            content={"checks": checks, "all_passed": all_passed, "failed_checks": failed_names},
            summary=f"Health check: {len(checks) - len(failed_names)}/{len(checks)} passed",
            quality_score=1.0 if all_passed else 0.0,
            artifacts=[artifact],
            failure_reason=None if all_passed else f"Failed checks: {failed_names}",
        )
