"""
services/devops/leads — L4 leads: ContainerLead, CicdLead, InfrastructureOpsLead.

Each team lead runs its workers in dependency-aware batches
(services.devops.context.topological_batches): tasks within a batch
run concurrently via asyncio.gather(); batches run sequentially.
Mirrors services/qa/leads and services/security/leads.
"""
from __future__ import annotations

import asyncio
from typing import Dict, List, Optional

import structlog

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.devops.context import (
    CICD_WORKERS,
    DEPLOYMENT_WORKERS,
    INFRASTRUCTURE_WORKERS,
    _CICD_DEPS,
    _DEPLOY_DEPS,
    _INFRA_DEPS,
)

log = structlog.get_logger(__name__)


def _artifact_type_of(a) -> Optional[str]:
    if isinstance(a, dict):
        return a.get("artifact_type")
    return getattr(a, "artifact_type", None)


def _ctx_record_result(task: TaskInput, worker_id: str, result: AgentResult) -> None:
    """Stores each worker's result under its worker_id (and artifact_type,
    first-writer-wins) so downstream workers/leads/head can read it —
    mirrors qa.leads._ctx_record_suite / security.leads._ctx_record_finding.
    """
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
            raise ValueError(f"Dependency cycle among DevOps workers: {remaining}")

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


@AgentFactory.register("container_lead")
class ContainerLead(BaseAgent):
    """Coordinates the Dockerfile Writer and Docker Compose workers."""

    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        results = await _run_batches(task, factory, INFRASTRUCTURE_WORKERS, _INFRA_DEPS)
        all_arts, failures, min_score = _aggregate(results)

        status = TaskStatus.FAILED if failures else TaskStatus.COMPLETED
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=status,
            content={"team": "infrastructure", "generated": len(results), "failures": failures},
            summary=f"Infrastructure generation: {len(results)} artifact(s), {len(failures)} failure(s)",
            quality_score=min_score, artifacts=all_arts,
            failure_reason="; ".join(failures) if failures else None,
        )


@AgentFactory.register("cicd_lead")
class CicdLead(BaseAgent):
    """Coordinates the Pipeline Config and Environment Config workers."""

    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        results = await _run_batches(task, factory, CICD_WORKERS, _CICD_DEPS)
        all_arts, failures, min_score = _aggregate(results)

        status = TaskStatus.FAILED if failures else TaskStatus.COMPLETED
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=status,
            content={"team": "cicd", "generated": len(results), "failures": failures},
            summary=f"CI/CD generation: {len(results)} artifact(s), {len(failures)} failure(s)",
            quality_score=min_score, artifacts=all_arts,
            failure_reason="; ".join(failures) if failures else None,
        )


@AgentFactory.register("infrastructure_ops_lead")
class InfrastructureOpsLead(BaseAgent):
    """
    Coordinates the Provisioner and Health Check workers — the spec's
    "Deployment Lead" responsibilities (see docs/M3.6 handover,
    Department Structure deviation note).
    """

    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        results = await _run_batches(task, factory, DEPLOYMENT_WORKERS, _DEPLOY_DEPS)
        all_arts, failures, min_score = _aggregate(results)

        status = TaskStatus.FAILED if failures else TaskStatus.COMPLETED
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=status,
            content={"team": "deployment", "executed": len(results), "failures": failures},
            summary=f"Deployment execution: {len(results)} step(s), {len(failures)} failure(s)",
            quality_score=min_score, artifacts=all_arts,
            failure_reason="; ".join(failures) if failures else None,
        )
