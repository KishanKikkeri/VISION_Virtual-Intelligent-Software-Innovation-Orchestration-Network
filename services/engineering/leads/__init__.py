"""
services/engineering/leads — L4 leads: BackendLead, FrontendLead,
IntegrationLead, ReviewLead (registered as "code_review_lead").

Each team lead runs its workers in dependency-aware batches
(services.engineering.context.topological_batches): tasks within a
batch run concurrently via asyncio.gather(); batches run sequentially.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import structlog

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.engineering.context import (
    BACKEND_WORKERS,
    FRONTEND_WORKERS,
    INTEGRATION_WORKERS,
    _BACKEND_DEPS,
    _FRONTEND_DEPS,
    _INTEGRATION_DEPS,
)
from services.engineering.routing import MAX_REVIEW_CYCLES

log = structlog.get_logger(__name__)


def _ctx_record_module(task: TaskInput, worker_id: str, result: AgentResult) -> None:
    """Stores each worker's generated module under its worker_id so downstream
    workers (e.g. messaging_worker reading internal_integration_worker's output)
    and the Review Lead can find it."""
    if result.content and isinstance(result.content, dict):
        task.context.approved_artifacts[worker_id] = result.content
        pending = task.context.approved_artifacts.setdefault("__pending_modules__", [])
        pending.append({**result.content, "generated_by": worker_id})


async def _run_batches(task: TaskInput, factory, worker_ids: List[str], deps: Dict[str, List[str]]) -> List[AgentResult]:
    """Runs a set of workers in dependency-ordered, concurrent batches."""
    remaining = list(worker_ids)
    done: set = set()
    results: List[AgentResult] = []

    while remaining:
        batch = [w for w in remaining if all(d in done for d in deps.get(w, []))]
        if not batch:
            raise ValueError(f"Dependency cycle among workers: {remaining}")

        async def _run_one(wid: str) -> AgentResult:
            if not factory:
                return AgentResult(task_id=task.task_id, agent_id=wid, status=TaskStatus.COMPLETED,
                                    content={"placeholder": True}, summary=f"{wid} placeholder", quality_score=0.8)
            r = await factory.create(wid).run(task)
            _ctx_record_module(task, wid, r)
            return r

        batch_results = await asyncio.gather(*[_run_one(w) for w in batch])
        results.extend(batch_results)
        for w in batch:
            done.add(w)
            remaining.remove(w)

    return results


@AgentFactory.register("backend_lead")
class BackendLead(BaseAgent):
    """Coordinates Database, Auth, Business Logic, and API workers in dependency order."""

    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        results = await _run_batches(task, factory, BACKEND_WORKERS, _BACKEND_DEPS)

        all_arts, failures, min_score = [], [], 1.0
        for r in results:
            all_arts.extend(r.artifacts)
            min_score = min(min_score, r.quality_score)
            if r.status in (TaskStatus.FAILED, TaskStatus.ESCALATED):
                failures.append(f"{r.agent_id}: {r.failure_reason}")

        if failures:
            return self.escalate(task, f"Backend team failures: {failures}")

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"team": "backend", "modules": len(results)},
            summary=f"Backend pipeline complete: {len(results)} modules",
            quality_score=min_score, artifacts=all_arts,
        )


@AgentFactory.register("frontend_lead")
class FrontendLead(BaseAgent):
    """
    Coordinates Component, Page, State, and Routing workers.
    Refuses to run entirely if there is no approved ui_blueprint
    (per Stage 3: "No UI generation without it").
    """

    async def execute(self, task: TaskInput) -> AgentResult:
        if not task.context.get_artifact("ui_blueprint"):
            log.info("frontend_lead_skipped_no_ui_blueprint", project_id=task.project_id)
            return AgentResult(
                task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
                content={"team": "frontend", "modules": 0, "skipped": True,
                         "reason": "No ui_blueprint — frontend team skipped, not failed"},
                summary="Frontend team skipped: no ui_blueprint present",
                quality_score=1.0, artifacts=[],
            )

        factory = task.context.approved_artifacts.get("__factory__")
        results = await _run_batches(task, factory, FRONTEND_WORKERS, _FRONTEND_DEPS)

        all_arts, failures, min_score = [], [], 1.0
        for r in results:
            all_arts.extend(r.artifacts)
            min_score = min(min_score, r.quality_score)
            if r.status in (TaskStatus.FAILED, TaskStatus.ESCALATED):
                failures.append(f"{r.agent_id}: {r.failure_reason}")

        if failures:
            return self.escalate(task, f"Frontend team failures: {failures}")

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"team": "frontend", "modules": len(results)},
            summary=f"Frontend pipeline complete: {len(results)} modules",
            quality_score=min_score, artifacts=all_arts,
        )


@AgentFactory.register("integration_lead")
class IntegrationLead(BaseAgent):
    """Coordinates Internal Event, External API, and Messaging workers."""

    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        results = await _run_batches(task, factory, INTEGRATION_WORKERS, _INTEGRATION_DEPS)

        all_arts, failures, min_score = [], [], 1.0
        for r in results:
            all_arts.extend(r.artifacts)
            min_score = min(min_score, r.quality_score)
            if r.status in (TaskStatus.FAILED, TaskStatus.ESCALATED):
                failures.append(f"{r.agent_id}: {r.failure_reason}")

        if failures:
            log.warning("integration_partial_failures", failures=failures)

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"team": "integration", "modules": len(results), "failures": failures},
            summary=f"Integration pipeline: {len(results)} modules, {len(failures)} failures",
            quality_score=min_score, artifacts=all_arts,
        )


@AgentFactory.register("code_review_lead")
class ReviewLead(BaseAgent):
    """
    Review Lead — mandatory gate. For every pending module:
      Code Review Worker -> (if blocking, bounded) Refactor Worker -> re-review
      -> Quality Worker (coding-contract gate)
    Modules that pass both gates go into __reviewed_modules__.
    Once all pending modules are resolved, runs Commit Worker exactly once
    to push everything to Repository Service via an integration branch + PR.

    No agent may approve artifacts it generated — Code Review Worker never
    reviews its own output; it only reviews modules built by other workers.
    """

    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        pending: List[Dict[str, Any]] = task.context.approved_artifacts.get("__pending_modules__", [])

        if not pending:
            return AgentResult(
                task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
                content={"team": "review", "reviewed": 0, "reason": "no pending modules"},
                summary="Review Lead: nothing to review", quality_score=1.0,
            )

        reviewed: List[Dict[str, Any]] = []
        blocked: List[str] = []
        all_arts = []

        for module in pending:
            passed_module = await self._review_one(task, factory, module)
            if passed_module is not None:
                reviewed.append(passed_module)
            else:
                blocked.append(module.get("generated_by", "unknown"))

        if not reviewed:
            return self.escalate(task, f"Review Lead: all modules blocked — {blocked}")

        task.context.approved_artifacts["__reviewed_modules__"] = reviewed

        if factory:
            commit_result = await factory.create("commit_worker").run(task)
            all_arts.extend(commit_result.artifacts)
            if commit_result.status in (TaskStatus.FAILED, TaskStatus.ESCALATED):
                return self.escalate(task, f"Commit Worker failed: {commit_result.failure_reason}")
        else:
            commit_result = AgentResult(task_id=task.task_id, agent_id="commit_worker",
                                         status=TaskStatus.COMPLETED, content={"placeholder": True},
                                         summary="commit placeholder", quality_score=0.8)

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"team": "review", "reviewed": len(reviewed), "blocked": blocked,
                     **({} if not isinstance(commit_result.content, dict) else commit_result.content)},
            summary=f"Review complete: {len(reviewed)} passed, {len(blocked)} blocked",
            quality_score=1.0 if not blocked else 0.75,
            artifacts=all_arts,
        )

    async def _review_one(self, task: TaskInput, factory, module: Dict[str, Any]):
        task.context.approved_artifacts["__current_module__"] = module
        cycles = 0
        current = module
        while cycles < MAX_REVIEW_CYCLES:
            cycles += 1
            if factory:
                review_result = await factory.create("code_reviewer_worker").run(task)
            else:
                review_result = AgentResult(task_id=task.task_id, agent_id="code_reviewer_worker",
                    status=TaskStatus.COMPLETED, content={"review_passed": True}, quality_score=0.9)

            if review_result.content.get("review_passed", review_result.status == TaskStatus.COMPLETED):
                # Quality gate (coding contract)
                task.context.approved_artifacts["__current_module__"] = current
                if factory:
                    quality_result = await factory.create("quality_worker").run(task)
                else:
                    quality_result = AgentResult(task_id=task.task_id, agent_id="quality_worker",
                        status=TaskStatus.COMPLETED, content={"coding_contract_satisfied": True}, quality_score=1.0)

                if quality_result.content.get("coding_contract_satisfied", True):
                    return current
                # Contract violated — one refactor attempt, then give up on this module
                task.context.approved_artifacts["__review_feedback__"] = {
                    "issues": [{"severity": "blocking", "description": v}
                               for v in quality_result.content.get("violations", [])]
                }
            else:
                task.context.approved_artifacts["__review_feedback__"] = review_result.content

            if factory:
                refactor_result = await factory.create("refactor_worker").run(task)
            else:
                refactor_result = AgentResult(task_id=task.task_id, agent_id="refactor_worker",
                    status=TaskStatus.COMPLETED, content={"files_modified": []}, quality_score=0.85)
            current = {**current, "quality_score": refactor_result.quality_score}

        return None   # exhausted MAX_REVIEW_CYCLES — module blocked
