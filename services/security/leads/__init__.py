"""
services/security/leads — L4 leads: DependencyScanLead, CodeSecurityLead,
ComplianceLead.

Each team lead runs its workers in dependency-aware batches
(services.security.context.topological_batches): tasks within a batch
run concurrently via asyncio.gather(); batches run sequentially.
Mirrors services/qa/leads exactly.
"""
from __future__ import annotations

import asyncio
from typing import Dict, List, Optional

import structlog

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.security.context import (
    CODE_WORKERS,
    COMPLIANCE_WORKERS,
    DEPENDENCY_WORKERS,
    _CODE_DEPS,
    _COMPLIANCE_DEPS,
    _DEPENDENCY_DEPS,
)

log = structlog.get_logger(__name__)


def _artifact_type_of(a) -> Optional[str]:
    if isinstance(a, dict):
        return a.get("artifact_type")
    return getattr(a, "artifact_type", None)


def _ctx_record_finding(task: TaskInput, worker_id: str, result: AgentResult) -> None:
    """
    Stores each worker's findings under its worker_id and the shared
    artifact_type key so downstream workers/leads (and SecurityHead's
    aggregation) can find it, mirroring qa.leads._ctx_record_suite.
    Deliberately keyed by worker_id (not just artifact_type) because
    owasp_checker_worker and injection_check_worker share the
    "static_analysis_report" artifact_type — SecurityHead reads both
    worker-specific keys to merge them.
    """
    if result.content and isinstance(result.content, dict):
        task.context.approved_artifacts[worker_id] = result.content
        for a in result.artifacts:
            t = _artifact_type_of(a)
            if t:
                task.context.approved_artifacts.setdefault(t, result.content)
        pending = task.context.approved_artifacts.setdefault("__pending_findings__", [])
        pending.append({**result.content, "generated_by": worker_id})


async def _run_batches(task: TaskInput, factory, worker_ids: List[str], deps: Dict[str, List[str]]) -> List[AgentResult]:
    remaining = list(worker_ids)
    done: set = set()
    results: List[AgentResult] = []

    while remaining:
        batch = [w for w in remaining if all(d in done for d in deps.get(w, []))]
        if not batch:
            raise ValueError(f"Dependency cycle among Security workers: {remaining}")

        async def _run_one(wid: str) -> AgentResult:
            if not factory:
                return AgentResult(task_id=task.task_id, agent_id=wid, status=TaskStatus.COMPLETED,
                                    content={"placeholder": True}, summary=f"{wid} placeholder", quality_score=0.8)
            r = await factory.create(wid).run(task)
            _ctx_record_finding(task, wid, r)
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


@AgentFactory.register("dependency_scan_lead")
class DependencyScanLead(BaseAgent):
    """Coordinates the CVE Scanner worker."""

    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        results = await _run_batches(task, factory, DEPENDENCY_WORKERS, _DEPENDENCY_DEPS)
        all_arts, failures, min_score = _aggregate(results)

        if failures:
            # Hard-fail findings are blocking-but-reportable, not a hard
            # escalation — SecurityHead's gate logic (services.security.
            # context.build_security_report) is the single place that
            # turns this into a SecurityFinding / FAIL verdict.
            log.warning("dependency_scan_lead_failures", failures=failures)

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"team": "dependency", "scans": len(results), "failures": failures},
            summary=f"Dependency scan pipeline: {len(results)} scan(s), {len(failures)} failure(s)",
            quality_score=min_score, artifacts=all_arts,
        )


@AgentFactory.register("code_security_lead")
class CodeSecurityLead(BaseAgent):
    """Coordinates OWASP Checker, Secret Scanner, and Injection Check workers."""

    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        results = await _run_batches(task, factory, CODE_WORKERS, _CODE_DEPS)
        all_arts, failures, min_score = _aggregate(results)

        if failures:
            log.warning("code_security_lead_failures", failures=failures)

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"team": "code_security", "scans": len(results), "failures": failures},
            summary=f"Code security pipeline: {len(results)} scan(s), {len(failures)} failure(s)",
            quality_score=min_score, artifacts=all_arts,
        )


@AgentFactory.register("compliance_lead")
class ComplianceLead(BaseAgent):
    """Coordinates the Compliance Validator worker."""

    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        results = await _run_batches(task, factory, COMPLIANCE_WORKERS, _COMPLIANCE_DEPS)
        all_arts, failures, min_score = _aggregate(results)

        if failures:
            log.warning("compliance_lead_failures", failures=failures)

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"team": "compliance", "scans": len(results), "failures": failures},
            summary=f"Compliance pipeline: {len(results)} scan(s), {len(failures)} failure(s)",
            quality_score=min_score, artifacts=all_arts,
        )
