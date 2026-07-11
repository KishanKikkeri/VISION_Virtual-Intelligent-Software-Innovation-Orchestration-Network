"""
services/qa/leads — L4 leads: UnitTestLead, IntegrationTestLead,
RegressionTestLead, PerformanceTestLead.

Each team lead runs its workers in dependency-aware batches
(services.qa.context.topological_batches): tasks within a batch run
concurrently via asyncio.gather(); batches run sequentially. Mirrors
services/engineering/leads exactly.
"""
from __future__ import annotations

import asyncio
from typing import Dict, List, Optional

import structlog

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.qa.context import (
    INTEGRATION_WORKERS,
    PERFORMANCE_WORKERS,
    REGRESSION_WORKERS,
    UNIT_WORKERS,
    _INTEGRATION_DEPS,
    _PERFORMANCE_DEPS,
    _REGRESSION_DEPS,
    _UNIT_DEPS,
)

log = structlog.get_logger(__name__)


def _artifact_type_of(a) -> Optional[str]:
    if isinstance(a, dict):
        return a.get("artifact_type")
    return getattr(a, "artifact_type", None)


def _ctx_record_suite(task: TaskInput, worker_id: str, result: AgentResult) -> None:
    """Stores each worker's generated suite under its worker_id and the shared
    artifact_type key so downstream workers/leads (and QAHead's aggregation)
    can find it, mirroring engineering.leads._ctx_record_module."""
    if result.content and isinstance(result.content, dict):
        task.context.approved_artifacts[worker_id] = result.content
        for a in result.artifacts:
            t = _artifact_type_of(a)
            if t:
                task.context.approved_artifacts[t] = result.content
        pending = task.context.approved_artifacts.setdefault("__pending_suites__", [])
        pending.append({**result.content, "generated_by": worker_id})


async def _run_batches(task: TaskInput, factory, worker_ids: List[str], deps: Dict[str, List[str]]) -> List[AgentResult]:
    remaining = list(worker_ids)
    done: set = set()
    results: List[AgentResult] = []

    while remaining:
        batch = [w for w in remaining if all(d in done for d in deps.get(w, []))]
        if not batch:
            raise ValueError(f"Dependency cycle among QA workers: {remaining}")

        async def _run_one(wid: str) -> AgentResult:
            if not factory:
                return AgentResult(task_id=task.task_id, agent_id=wid, status=TaskStatus.COMPLETED,
                                    content={"placeholder": True}, summary=f"{wid} placeholder", quality_score=0.8)
            r = await factory.create(wid).run(task)
            _ctx_record_suite(task, wid, r)
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


@AgentFactory.register("unit_test_lead")
class UnitTestLead(BaseAgent):
    """Coordinates Unit Test Writer and Coverage Analyzer workers."""

    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        results = await _run_batches(task, factory, UNIT_WORKERS, _UNIT_DEPS)
        all_arts, failures, min_score = _aggregate(results)

        if failures:
            # Coverage gaps are blocking-but-reportable, not a hard escalation —
            # QAHead's gate logic (services.qa.context.build_qa_report) is the
            # single place that turns this into a DefectReport / FAIL verdict.
            log.warning("unit_test_lead_failures", failures=failures)

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"team": "unit", "suites": len(results), "failures": failures},
            summary=f"Unit test pipeline: {len(results)} suite(s), {len(failures)} failure(s)",
            quality_score=min_score, artifacts=all_arts,
        )


@AgentFactory.register("integration_test_lead")
class IntegrationTestLead(BaseAgent):
    """Coordinates the Integration/API Test Writer worker."""

    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        results = await _run_batches(task, factory, INTEGRATION_WORKERS, _INTEGRATION_DEPS)
        all_arts, failures, min_score = _aggregate(results)

        if failures:
            log.warning("integration_test_lead_failures", failures=failures)

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"team": "integration", "suites": len(results), "failures": failures},
            summary=f"Integration test pipeline: {len(results)} suite(s), {len(failures)} failure(s)",
            quality_score=min_score, artifacts=all_arts,
        )


@AgentFactory.register("regression_test_lead")
class RegressionTestLead(BaseAgent):
    """Coordinates the Regression Suite worker."""

    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        results = await _run_batches(task, factory, REGRESSION_WORKERS, _REGRESSION_DEPS)
        all_arts, failures, min_score = _aggregate(results)

        if failures:
            log.warning("regression_test_lead_failures", failures=failures)

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"team": "regression", "suites": len(results), "failures": failures},
            summary=f"Regression pipeline: {len(results)} suite(s), {len(failures)} failure(s)",
            quality_score=min_score, artifacts=all_arts,
        )


@AgentFactory.register("performance_test_lead")
class PerformanceTestLead(BaseAgent):
    """Coordinates the Performance Test worker."""

    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        results = await _run_batches(task, factory, PERFORMANCE_WORKERS, _PERFORMANCE_DEPS)
        all_arts, failures, min_score = _aggregate(results)

        if failures:
            log.warning("performance_test_lead_failures", failures=failures)

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"team": "performance", "suites": len(results), "failures": failures},
            summary=f"Performance pipeline: {len(results)} suite(s), {len(failures)} failure(s)",
            quality_score=min_score, artifacts=all_arts,
        )
