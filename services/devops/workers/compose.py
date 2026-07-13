"""services/devops/workers/compose.py — Docker Compose Worker."""
from __future__ import annotations

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.devops.models import ComposeArtifact
from services.devops.utils import render_compose


@AgentFactory.register("docker_compose_worker")
class DockerComposeWorker(BaseAgent):
    """Produces docker-compose.yml. Deterministic, template-based — no LLM call."""

    async def execute(self, task: TaskInput) -> AgentResult:
        port = task.context.approved_artifacts.get("__exposed_port_override__", 8000)
        content, services = render_compose(task.context.project_name or "app", exposed_port=port)
        artifact_model = ComposeArtifact(project_id=task.project_id, content=content, services=services)

        artifact = await self.create_artifact(task, "docker_compose", artifact_model.model_dump())
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content=artifact_model.model_dump(),
            summary=f"Generated docker-compose.yml ({len(services)} service(s))",
            quality_score=0.9, artifacts=[artifact],
        )
