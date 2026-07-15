"""
services/incident_response/leads — L4 leads: IncidentAnalysisLead,
RecoveryLead, CommunicationLead.

Each team lead runs its workers in dependency-aware batches
(services.incident_response.context.topological_batches / the
_ANALYSIS_DEPS / _RECOVERY_DEPS / _COMMUNICATION_DEPS maps) — mirrors
services/monitoring/leads exactly (asyncio.gather within a batch,
sequential across batches).
"""
from __future__ import annotations

import asyncio
from typing import Dict, List, Optional

import structlog

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.incident_response.context import (
    ANALYSIS_WORKERS,
    COMMUNICATION_WORKERS,
    RECOVERY_WORKERS,
    _ANALYSIS_DEPS,
    _COMMUNICATION_DEPS,
    _RECOVERY_DEPS,
)

log = structlog.get_logger(__name__)


def _artifact_type_of(a) -> Optional[str]:
    if isinstance(a, dict):
        return a.get("artifact_type")
    return getattr(a, "artifact_type", None)


def _ctx_record_result(task: TaskInput, worker_id: str, result: AgentResult) -> None:
    """Mirrors services.monitoring.leads._ctx_record_result — stores each
    worker's result under its worker_id so any downstream lead/head can
    read it back, regardless of which team ran it."""
    if result.content and isinstance(result.content, dict):
        task.context.approved_artifacts[worker_id] = result.content
        for a in result.artifacts:
            t = _artifact_type_of(a)
            if t:
                task.context.approved_artifacts.setdefault(t, result.content)


async def _run_batches(task: TaskInput, factory, worker_ids: List[str], deps: Dict[str, List[str]]) -> List[AgentResult]:
    remaining = list(worker_ids)
    done: set = set()
    results: List[AgentResult] = []

    while remaining:
        batch = [w for w in remaining if all(d in done for d in deps.get(w, []))]
        if not batch:
            raise ValueError(f"Dependency cycle among Incident Response workers: {remaining}")

        async def _run_one(wid: str) -> AgentResult:
            if not factory:
                return AgentResult(task_id=task.task_id, agent_id=wid, status=TaskStatus.COMPLETED,
                                    content={"placeholder": True}, summary=f"{wid} placeholder", quality_score=0.8)
            r = await factory.create(wid).run(task)
            _ctx_record_result(task, wid, r)
            return r

        batch_results = await asyncio.gather(*[_run_one(w) for w in batch])
        results.extend(batch_results)
        for w in batch:
            done.add(w)
            remaining.remove(w)

    return results


def _aggregate(results: List[AgentResult]):
    all_arts, failures, min_score = [], [], 1.0
    for r in results:
        all_arts.extend(r.artifacts)
        min_score = min(min_score, r.quality_score)
        if r.status in (TaskStatus.FAILED, TaskStatus.ESCALATED):
            failures.append(f"{r.agent_id}: {r.failure_reason}")
    return all_arts, failures, min_score


@AgentFactory.register("incident_analysis_lead")
class IncidentAnalysisLead(BaseAgent):
    """Coordinates Incident Classifier Worker + Evidence Collection Worker."""

    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        results = await _run_batches(task, factory, ANALYSIS_WORKERS, _ANALYSIS_DEPS)
        all_arts, failures, min_score = _aggregate(results)

        classification = task.context.approved_artifacts.get(
            "incident_classifier_worker", {}).get("classification", {})

        status = TaskStatus.FAILED if failures else TaskStatus.COMPLETED
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=status,
            content={"team": "incident_analysis", "recommended_action": classification.get("recommended_action"),
                      "failures": failures},
            summary=f"Analysis complete: action={classification.get('recommended_action')}",
            quality_score=min_score, artifacts=all_arts,
            failure_reason="; ".join(failures) if failures else None,
        )


@AgentFactory.register("recovery_lead")
class RecoveryLead(BaseAgent):
    """Coordinates Rollback Worker + Recovery Worker; owns the recovery_plan artifact."""

    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        results = await _run_batches(task, factory, RECOVERY_WORKERS, _RECOVERY_DEPS)
        all_arts, failures, min_score = _aggregate(results)

        recovery_status = task.context.approved_artifacts.get(
            "recovery_worker", {}).get("recovery_status", "skipped")

        status = TaskStatus.FAILED if failures else TaskStatus.COMPLETED
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=status,
            content={"team": "recovery", "recovery_status": recovery_status, "failures": failures},
            summary=f"Recovery: status={recovery_status}",
            quality_score=min_score, artifacts=all_arts,
            failure_reason="; ".join(failures) if failures else None,
        )


@AgentFactory.register("communication_lead")
class CommunicationLead(BaseAgent):
    """Coordinates Notification Worker + Reporting Worker (independent — no shared deps)."""

    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        results = await _run_batches(task, factory, COMMUNICATION_WORKERS, _COMMUNICATION_DEPS)
        all_arts, failures, min_score = _aggregate(results)

        status = TaskStatus.FAILED if failures else TaskStatus.COMPLETED
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=status,
            content={"team": "communication", "executed": len(results), "failures": failures},
            summary=f"Communication: {len(results)} step(s), {len(failures)} failure(s)",
            quality_score=min_score, artifacts=all_arts,
            failure_reason="; ".join(failures) if failures else None,
        )
