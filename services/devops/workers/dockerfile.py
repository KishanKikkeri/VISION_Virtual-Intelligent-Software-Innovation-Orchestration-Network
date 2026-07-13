"""services/devops/workers/dockerfile.py — Dockerfile Writer Worker."""
from __future__ import annotations

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.devops.models import DockerfileArtifact
from services.devops.utils import render_dockerfile


@AgentFactory.register("dockerfile_writer_worker")
class DockerfileWriterWorker(BaseAgent):
    """
    Produces a Dockerfile for the approved codebase. Deterministic,
    template-based (see utils.render_dockerfile docstring) — no LLM call.
    """

    async def execute(self, task: TaskInput) -> AgentResult:
        tech_stack = task.context.tech_stack or {}
        port = task.context.approved_artifacts.get("__exposed_port_override__", 8000)

        content = render_dockerfile(tech_stack, exposed_port=port)
        artifact_model = DockerfileArtifact(project_id=task.project_id, content=content, exposed_port=port)

        artifact = await self.create_artifact(task, "dockerfile", artifact_model.model_dump())
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content=artifact_model.model_dump(),
            summary=f"Generated Dockerfile (base={artifact_model.base_image}, port={port})",
            quality_score=0.9, artifacts=[artifact],
        )
