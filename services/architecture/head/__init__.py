"""services/architecture/head — L3 ArchitectureHead: orchestrates the full pipeline."""
from __future__ import annotations
import structlog
from core.contracts import AgentResult, NATSEvent, TaskStatus, WebSocketEvent
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
log = structlog.get_logger(__name__)

# Artifacts submitted to the user for approval
APPROVAL_ARTIFACTS = [
    "architecture_blueprint",
    "api_spec",
    "database_schema",
    "deployment_architecture",
    "ui_blueprint",   # Appendix A (M3.3) — same approval gate, no new workflow
]

@AgentFactory.register("architecture_head")
class ArchitectureHead(BaseAgent):
    """
    L3 — Sole orchestrator of architecture-service.
    Pipeline (strict order):
      Phase A: System Design Lead (sequential: blueprint → api → db)
      Phase B: Platform Design Lead (parallel: infra + security + scaling + integration)
      Phase C: Review Lead (traceability → reviewer)
      Phase D: Artifact submission → manager approval gate

    On review escalation (traceability gap):
      Re-run System Design Lead with gap context injected (max 2 re-runs).

    On approval rejection:
      Targeted re-run of only the leads whose artifacts were flagged.
    """

    MAX_TRACEABILITY_RERUNS = 2

    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")

        await self.notify_ui(task.project_id, "phase_started", {
            "phase": 3, "phase_name": "Architecture Design",
            "message": "Architecture pipeline starting",
        })
        await self.publish_event("architecture.pipeline.started",
            {"project_id": task.project_id, "phase": 3})

        all_artifacts = []

        # ── Phase A: System Design (sequential) ──────────────────
        log.info("arch_head_phase_a", project_id=task.project_id)
        await self.notify_ui(task.project_id, "agent_started",
            {"agent": "system_design_lead", "step": "System design pipeline"})

        if factory:
            sys_lead = factory.create("system_design_lead")
            sys_result = await sys_lead.run(task)
        else:
            sys_result = _placeholder(task, "system_design_lead", "System design")

        all_artifacts.extend(sys_result.artifacts)
        _ctx_update(task, sys_result)

        if sys_result.status in (TaskStatus.FAILED, TaskStatus.ESCALATED):
            return self.escalate(task, f"System Design Lead failed: {sys_result.failure_reason}")

        await self.publish_event("architecture.system_design.completed",
            {"project_id": task.project_id,
             "components_count": len(task.context.get_artifact("architecture_blueprint",{}).get("components",[]))})

        # ── Phase B: Platform Design (parallel) ──────────────────
        log.info("arch_head_phase_b", project_id=task.project_id)
        await self.notify_ui(task.project_id, "agent_started",
            {"agent": "platform_design_lead", "step": "Parallel platform design"})

        if factory:
            plat_lead   = factory.create("platform_design_lead")
            plat_result = await plat_lead.run(task)
        else:
            plat_result = _placeholder(task, "platform_design_lead", "Platform design")

        all_artifacts.extend(plat_result.artifacts)
        _ctx_update(task, plat_result)

        if plat_result.status in (TaskStatus.FAILED, TaskStatus.ESCALATED):
            return self.escalate(task, f"Platform Design Lead failed: {plat_result.failure_reason}")

        # ── Phase C: Review (traceability + arch review) ─────────
        log.info("arch_head_phase_c", project_id=task.project_id)
        await self.notify_ui(task.project_id, "agent_started",
            {"agent": "architecture_review_lead", "step": "Traceability + review"})

        rev_result = await self._run_review_with_retry(task, factory)

        if rev_result.status in (TaskStatus.FAILED, TaskStatus.ESCALATED):
            await self.publish_event("architecture.pipeline.failed",
                {"project_id": task.project_id,
                 "reason": rev_result.failure_reason, "failed_at": "review_lead"})
            return self.escalate(task, f"Review Lead failed: {rev_result.failure_reason}")

        all_artifacts.extend(rev_result.artifacts)
        _ctx_update(task, rev_result)

        await self.publish_event("architecture.traceability.checked",
            {"project_id": task.project_id, "passed": True,
             "coverage_pct": rev_result.content.get("coverage_pct", 0)})

        # ── Phase D: Submit for approval ──────────────────────────
        log.info("arch_head_submit", project_id=task.project_id)
        submitted_ids = await self._submit_for_approval(task)

        await self.write_memory(task,
            f"Architecture complete for {task.project_id}: "
            f"{len(all_artifacts)} artifacts, "
            f"coverage={rev_result.content.get('coverage_pct',0):.1f}%",
            source="architecture_head")

        await self.publish_event("architecture.design.completed",
            {"project_id": task.project_id,
             "artifact_ids": submitted_ids,
             "requires_approval": True,
             "artifact_types": APPROVAL_ARTIFACTS})

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id,
            status=TaskStatus.COMPLETED,
            content={
                "phase":           "architecture",
                "status":          "submitted_for_approval",
                "artifacts_total": len(all_artifacts),
                "submitted_ids":   submitted_ids,
            },
            summary=(f"Architecture department complete — "
                     f"{len(submitted_ids)} artifacts submitted for approval"),
            quality_score=min(sys_result.quality_score,
                              plat_result.quality_score,
                              rev_result.quality_score),
            artifacts=all_artifacts,
            nats_events=[
                NATSEvent(
                    subject="architecture.design.completed",
                    payload={"project_id": task.project_id,
                             "artifact_ids": submitted_ids,
                             "requires_approval": True},
                    project_id=task.project_id,
                )
            ],
            ws_events=[
                WebSocketEvent(
                    project_id=task.project_id,
                    event_type="approval_required",
                    payload={
                        "artifact_type": "architecture",
                        "message":       "Architecture blueprint ready for your review",
                        "artifacts":     APPROVAL_ARTIFACTS,
                    },
                )
            ],
        )

    async def _run_review_with_retry(
        self, task: TaskInput, factory
    ) -> AgentResult:
        """
        Runs ReviewLead. If traceability escalation is raised (coverage gap),
        re-runs SystemDesignLead with the gap list injected, then retries review.
        Max 2 traceability re-runs.
        """
        for attempt in range(self.MAX_TRACEABILITY_RERUNS + 1):
            if factory:
                rev_lead   = factory.create("architecture_review_lead")
                rev_result = await rev_lead.run(task)
            else:
                return _placeholder(task, "architecture_review_lead", "Review", passed=True)

            # Check if escalation is a traceability gap
            if (rev_result.status == TaskStatus.ESCALATED
                    and "coverage" in (rev_result.failure_reason or "").lower()
                    and attempt < self.MAX_TRACEABILITY_RERUNS):

                log.warning("arch_head_traceability_gap",
                            attempt=attempt + 1,
                            reason=rev_result.failure_reason,
                            project_id=task.project_id)

                await self.notify_ui(task.project_id, "traceability_gap_detected", {
                    "attempt": attempt + 1,
                    "reason":  rev_result.failure_reason,
                    "action":  "Re-running system design with gap context",
                })

                # Inject gap context into task for targeted re-run
                task.context.approved_artifacts["__traceability_gaps__"] = {
                    "gaps":   rev_result.failure_reason,
                    "attempt": attempt + 1,
                }

                # Targeted re-run: system design lead only
                if factory:
                    sys_lead   = factory.create("system_design_lead")
                    sys_result = await sys_lead.run(task)
                    _ctx_update(task, sys_result)
                continue

            return rev_result

        # Exhausted retries — return last result
        return rev_result

    async def _submit_for_approval(self, task: TaskInput) -> list:
        """Transitions approval artifacts to under_review in DB."""
        submitted = []
        try:
            from sqlalchemy import select
            from infrastructure.database.models import Artifact
            async with self._db_factory() as db:
                for atype in APPROVAL_ARTIFACTS:
                    r = await db.execute(
                        select(Artifact)
                        .where(Artifact.project_id    == task.project_id,
                               Artifact.artifact_type == atype,
                               Artifact.status        == "draft")
                        .order_by(Artifact.version.desc())
                        .limit(1)
                    )
                    art = r.scalar_one_or_none()
                    if art:
                        art.status = "under_review"
                        submitted.append(art.id)
        except Exception as e:
            log.warning("arch_submit_db_error", error=str(e))
        return submitted


# ── Helpers ───────────────────────────────────────────────────

def _ctx_update(task, result):
    if result.content and isinstance(result.content, dict):
        for a in result.artifacts:
            if isinstance(a, dict) and a.get("artifact_type"):
                task.context.approved_artifacts[a["artifact_type"]] = result.content


def _placeholder(task, agent_id, step, passed=False):
    return AgentResult(
        task_id=task.task_id, agent_id=agent_id,
        status=TaskStatus.COMPLETED,
        content={"placeholder": True, "review_passed": passed,
                 "coverage_pct": 100.0, "overall_passed": passed},
        summary=f"{step} placeholder (no factory)",
        quality_score=0.8,
    )
