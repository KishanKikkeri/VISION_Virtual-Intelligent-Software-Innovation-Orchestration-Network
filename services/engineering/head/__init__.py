"""
services/engineering/head — L3 EngineeringHead: orchestrates the full
M3.3 pipeline.

Pipeline (matches the LangGraph in workflows/engineering_graph.py):
  Stage 1: Implementation Plan  — read approved Architecture artifacts
  Stage 2: Task Breakdown       — build the dependency-scheduled task graph
  Stage 3: Parallel Fan-Out     — Backend / Frontend / Integration leads run
                                  concurrently (Frontend skips itself if no
                                  ui_blueprint)
  Stage 4: Aggregate Results    — collect all pending modules
  Stage 5: Review Cycle         — Review Lead (mandatory gate) + Commit Worker
  Stage 6: Publish Artifacts    — engineering.phase.completed
"""
from __future__ import annotations

import asyncio

import structlog

from core.contracts import AgentResult, NATSEvent, TaskStatus, WebSocketEvent
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.engineering.context import build_implementation_plan

log = structlog.get_logger(__name__)

TEAM_LEADS = [
    ("backend_lead", "Backend"),
    ("frontend_lead", "Frontend"),
    ("integration_lead", "Integration"),
]


def _ctx_update(task, result):
    if result.content and isinstance(result.content, dict):
        for a in result.artifacts:
            if isinstance(a, dict) and a.get("artifact_type"):
                task.context.approved_artifacts[a["artifact_type"]] = result.content


@AgentFactory.register("engineering_head")
class EngineeringHead(BaseAgent):
    """L3 — Sole orchestrator of engineering-service."""

    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        feature_name = task.context.approved_artifacts.get("__feature_name__", "default")

        await self.notify_ui(task.project_id, "phase_started", {
            "phase": 4, "phase_name": "Engineering Implementation",
            "message": "Engineering pipeline starting",
        })
        await self.publish_event("engineering.pipeline.started",
            {"project_id": task.project_id, "feature_name": feature_name})

        # ── Stage 1 + 2: Implementation Plan + Task Breakdown ────
        plan = build_implementation_plan(
            project_id=task.project_id,
            feature_name=feature_name,
            architecture_refs=task.context.approved_artifacts,
        )
        task.context.approved_artifacts["__implementation_plan__"] = plan.model_dump()
        task.context.approved_artifacts["__feature_name__"] = feature_name
        await self.notify_ui(task.project_id, "plan_ready",
            {"plan_id": plan.plan_id, "total_tasks": len(plan.tasks)})

        # ── Stage 3: Parallel Fan-Out (Backend / Frontend / Integration) ──
        log.info("eng_head_fan_out", project_id=task.project_id)
        await self.notify_ui(task.project_id, "agent_started",
            {"agent": "parallel_fan_out", "step": "Backend + Frontend + Integration"})

        async def _run_lead(agent_id: str, step: str) -> AgentResult:
            if not factory:
                return AgentResult(task_id=task.task_id, agent_id=agent_id, status=TaskStatus.COMPLETED,
                                    content={"placeholder": True}, summary=f"{step} placeholder", quality_score=0.8)
            r = await factory.create(agent_id).run(task)
            await self.notify_ui(task.project_id, "agent_completed",
                {"agent": agent_id, "step": step, "status": r.status.value, "score": r.quality_score})
            return r

        lead_results = await asyncio.gather(*[_run_lead(a, s) for a, s in TEAM_LEADS])

        all_artifacts = []
        hard_failures = []
        for (agent_id, step), r in zip(TEAM_LEADS, lead_results):
            all_artifacts.extend(r.artifacts)
            _ctx_update(task, r)
            if r.status in (TaskStatus.FAILED, TaskStatus.ESCALATED) and agent_id != "frontend_lead":
                hard_failures.append(f"{step}: {r.failure_reason}")
            elif r.status in (TaskStatus.FAILED, TaskStatus.ESCALATED) and agent_id == "frontend_lead":
                # Frontend failing (not skipping) is still a hard stop — UI generation
                # was attempted and failed the coding contract.
                if not (isinstance(r.content, dict) and r.content.get("skipped")):
                    hard_failures.append(f"{step}: {r.failure_reason}")

        if hard_failures:
            await self.publish_event("engineering.pipeline.failed",
                {"project_id": task.project_id, "reason": hard_failures})
            return self.escalate(task, f"Parallel fan-out failed: {hard_failures}")

        # ── Stage 4: Aggregate Results ────────────────────────────
        pending = task.context.approved_artifacts.get("__pending_modules__", [])
        log.info("eng_head_aggregate", project_id=task.project_id, modules=len(pending))
        await self.publish_event("engineering.modules.aggregated",
            {"project_id": task.project_id, "modules_count": len(pending)})

        # ── Stage 5: Review Cycle (mandatory gate + Commit Worker) ─
        log.info("eng_head_review", project_id=task.project_id)
        await self.notify_ui(task.project_id, "agent_started",
            {"agent": "code_review_lead", "step": "Review cycle + Repository Service"})

        if factory:
            review_result = await factory.create("code_review_lead").run(task)
        else:
            review_result = AgentResult(task_id=task.task_id, agent_id="code_review_lead",
                status=TaskStatus.COMPLETED, content={"reviewed": len(pending)},
                summary="Review placeholder", quality_score=0.85)

        all_artifacts.extend(review_result.artifacts)
        _ctx_update(task, review_result)

        if review_result.status in (TaskStatus.FAILED, TaskStatus.ESCALATED):
            await self.publish_event("engineering.pipeline.failed",
                {"project_id": task.project_id, "reason": review_result.failure_reason,
                 "failed_at": "code_review_lead"})
            return self.escalate(task, f"Review Lead failed: {review_result.failure_reason}")

        # ── Stage 6: Publish Artifacts ─────────────────────────────
        rc = review_result.content or {}
        await self.write_memory(
            task,
            f"Engineering complete for {task.project_id}: {len(pending)} modules, "
            f"PR={rc.get('pull_request_id')}",
            source="engineering_head",
        )

        completed_event = {
            "project_id": task.project_id,
            "workflow_id": task.context.workflow_id,
            "feature_name": feature_name,
            "modules_total": len(pending),
            "pull_request_id": rc.get("pull_request_id"),
            "merge_sha": rc.get("merge_sha"),
        }
        await self.publish_event("engineering.phase.completed", completed_event)

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={
                "phase": "engineering",
                "status": "complete",
                "modules_total": len(pending),
                "plan_id": plan.plan_id,
                **rc,
            },
            summary=f"Engineering department complete — {len(pending)} modules, "
                    f"PR {rc.get('pull_request_id', 'n/a')}",
            quality_score=review_result.quality_score,
            artifacts=all_artifacts,
            nats_events=[
                NATSEvent(subject="engineering.phase.completed", payload=completed_event,
                          project_id=task.project_id),
            ],
            ws_events=[
                WebSocketEvent(project_id=task.project_id, event_type="phase_completed",
                                payload={"phase": 4, "phase_name": "Engineering Implementation",
                                         "message": "Implementation complete — merge-ready PR opened"}),
            ],
        )
