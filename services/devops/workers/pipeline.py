"""
services/devops/workers/pipeline.py — Pipeline Config Worker.

Satisfies both the spec's "GitHub Actions Worker" and "Pipeline Worker"
responsibilities in one registered agent (see docs/M3.6 handover,
Department Structure deviation note).
"""
from __future__ import annotations

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.devops.models import PipelineConfigArtifact
from services.devops.utils import render_github_actions


@AgentFactory.register("pipeline_config_worker")
class PipelineConfigWorker(BaseAgent):
    """Produces .github/workflows/build.yml. Deterministic, template-based — no LLM call."""

    async def execute(self, task: TaskInput) -> AgentResult:
        content = render_github_actions(task.context.project_name or "app")
        artifact_model = PipelineConfigArtifact(project_id=task.project_id, content=content)

        artifact = await self.create_artifact(task, "pipeline_config", artifact_model.model_dump())
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content=artifact_model.model_dump(),
            summary="Generated GitHub Actions CI/CD workflow",
            quality_score=0.9, artifacts=[artifact],
        )
