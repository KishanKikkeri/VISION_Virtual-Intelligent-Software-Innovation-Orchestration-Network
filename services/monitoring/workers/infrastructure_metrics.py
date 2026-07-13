"""services/monitoring/workers/infrastructure_metrics.py — Infrastructure Metrics Worker.

Collects Postgres / Qdrant / NATS / Docker samples via providers (spec §1/§6).
"""
from __future__ import annotations

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.monitoring.integration.monitoring_repository import (
    MetricRepository, MetricSampleRepository,
)
from services.monitoring.providers import infrastructure_providers


@AgentFactory.register("infrastructure_metrics_worker")
class InfrastructureMetricsWorker(BaseAgent):
    """
    Deterministic collection worker — no LLM call. Failures in any one
    provider degrade that component to 0 for this cycle rather than
    aborting the whole collection (spec §7/§8).
    """

    async def execute(self, task: TaskInput) -> AgentResult:
        providers = infrastructure_providers(self._db_factory, self._qdrant, self._nats)

        all_samples = []
        for provider in providers:
            try:
                samples = await provider.collect()
            except Exception as e:  # providers should not raise, but never trust that fully
                samples = provider._degraded(str(e))
            all_samples.extend(samples)

        # Persist each sample (best-effort — a write failure here degrades
        # nothing about the in-memory cycle result; the graph still sees
        # the freshly-collected values).
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
            summary=f"Collected {len(all_samples)} infrastructure metric samples",
            quality_score=1.0,
        )
