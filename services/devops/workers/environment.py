"""services/devops/workers/environment.py — Environment Config Worker."""
from __future__ import annotations

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.devops.models import EnvironmentConfigArtifact
from services.devops.utils import render_env_example


@AgentFactory.register("environment_config_worker")
class EnvironmentConfigWorker(BaseAgent):
    """
    Produces .env.example + runtime configuration. Deterministic — no
    LLM call. Reads openapi_spec/database_schema (if present) to size
    a couple of derived settings (see utils.render_env_example).
    """

    async def execute(self, task: TaskInput) -> AgentResult:
        openapi_spec = task.context.get_artifact("openapi_spec", {})
        database_schema = task.context.get_artifact("database_schema", {})

        content, variables = render_env_example(openapi_spec, database_schema)
        artifact_model = EnvironmentConfigArtifact(project_id=task.project_id, content=content, variables=variables)

        artifact = await self.create_artifact(task, "environment_config", artifact_model.model_dump())
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content=artifact_model.model_dump(),
            summary=f"Generated .env.example ({len(variables)} variable(s))",
            quality_score=0.9, artifacts=[artifact],
        )
