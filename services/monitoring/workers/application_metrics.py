"""services/monitoring/workers/application_metrics.py — Application Metrics Worker.

Collects agent runtime / LLM / WebSocket / repository / deployment
samples via providers (spec §1/§6).
"""
from __future__ import annotations

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.monitoring.integration.monitoring_repository import (
    MetricRepository, MetricSampleRepository,
)
from services.monitoring.providers import application_providers


@AgentFactory.register("application_metrics_worker")
class ApplicationMetricsWorker(BaseAgent):
    """Deterministic collection worker — no LLM call. See InfrastructureMetricsWorker docstring."""

    async def execute(self, task: TaskInput) -> AgentResult:
        providers = application_providers(self._db_factory)

        all_samples = []
        for provider in providers:
            try:
                samples = await provider.collect()
            except Exception as e:
                samples = provider._degraded(str(e))
            all_samples.extend(samples)

        try:
            async with self._db_factory() as db:
                for s in all_samples:
                    metric = await MetricRepository.get_or_create(db, s.name, s.component.value, s.unit)
                    await MetricSampleRepository.record(
                        db, metric.id, s.value, labels=s.labels, project_id=s.project_id)
        except Exception:
            pass

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"samples": [s.model_dump(mode="json") for s in all_samples]},
            summary=f"Collected {len(all_samples)} application metric samples",
            quality_score=1.0,
        )
