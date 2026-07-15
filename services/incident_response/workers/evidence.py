"""services/incident_response/workers/evidence.py — Evidence Collection Worker.

Deterministic — no LLM call. Consults every EvidenceProvider (Monitoring/
Repository/DevOps, spec §8/§9 read-only rule) and persists each item to
the Incident Response Service's own `incident_evidence` table.
"""
from __future__ import annotations

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.incident_response.integration.incident_repository import IncidentEvidenceRepository
from services.incident_response.providers import evidence_providers


@AgentFactory.register("evidence_collection_worker")
class EvidenceCollectionWorker(BaseAgent):
    """Deterministic — no LLM call."""

    async def execute(self, task: TaskInput) -> AgentResult:
        arts = task.context.approved_artifacts
        component = arts.get("__component__")
        incident_id = arts.get("__incident_id__")

        providers = evidence_providers(self._db_factory)
        all_items = []
        for provider in providers:
            items = await provider.collect(component)
            all_items.extend(items)

        for item in all_items:
            try:
                async with self._db_factory() as db:
                    await IncidentEvidenceRepository.record(
                        db, incident_id, item.source.value, item.ref, item.summary)
            except Exception:
                pass

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"evidence": [i.model_dump(mode="json") for i in all_items]},
            summary=f"Collected {len(all_items)} evidence item(s)",
            quality_score=1.0,
        )
