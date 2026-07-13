"""services/architecture/leads — L4 leads: SystemDesignLead, PlatformDesignLead, ReviewLead."""
from __future__ import annotations
import asyncio
import structlog
from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
log = structlog.get_logger(__name__)

def _ctx_update(task, result):
    if result.content and isinstance(result.content, dict):
        for a in result.artifacts:
            if isinstance(a, dict) and a.get("artifact_type"):
                task.context.approved_artifacts[a["artifact_type"]] = result.content


@AgentFactory.register("system_design_lead")
class SystemDesignLead(BaseAgent):
    """Sequential: blueprint → api_spec → database_schema (each feeds next)."""
    PIPELINE = [
        ("system_architect_worker",   "Architecture blueprint"),
        ("openapi_spec_writer_worker", "OpenAPI 3.1 spec"),
        ("schema_designer_worker",    "Database schema"),
    ]
    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        all_arts, min_score = [], 1.0
        for aid, step in self.PIPELINE:
            log.info("sys_design_step", step=step, project_id=task.project_id)
            if factory:
                r = await factory.create(aid).run(task)
            else:
                r = AgentResult(task_id=task.task_id, agent_id=aid, status=TaskStatus.COMPLETED,
                    content={"placeholder":True}, summary=f"{step} placeholder", quality_score=0.8)
            all_arts.extend(r.artifacts); min_score = min(min_score, r.quality_score)
            _ctx_update(task, r)
            if r.status in (TaskStatus.FAILED, TaskStatus.ESCALATED):
                return self.escalate(task, f"System Design: '{step}' failed → {r.failure_reason}")
            await self.notify_ui(task.project_id, "agent_completed",
                {"agent":aid,"step":step,"score":r.quality_score,"artifacts":len(r.artifacts)})
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"system_design":"complete"}, summary=f"System Design complete: {len(all_arts)} artifacts",
            quality_score=min_score, artifacts=all_arts)


@AgentFactory.register("platform_design_lead")
class PlatformDesignLead(BaseAgent):
    """Parallel: infra + security + scaling + integration run concurrently."""
    WORKERS = [
        ("infrastructure_planner_worker", "Infrastructure & deployment"),
        ("security_architect_worker",     "Security architecture"),
        ("scalability_architect_worker",  "Scaling strategy"),
        ("integration_architect_worker",  "Integration plan"),
        ("ui_architect_worker",           "UI blueprint"),   # Appendix A (M3.3)
    ]
    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")

        async def _run(aid, step):
            log.info("platform_worker_start", worker=aid, project_id=task.project_id)
            if not factory:
                return AgentResult(task_id=task.task_id, agent_id=aid, status=TaskStatus.COMPLETED,
                    content={"placeholder":True}, summary=f"{step} placeholder", quality_score=0.8)
            r = await factory.create(aid).run(task)
            await self.notify_ui(task.project_id,"agent_completed",
                {"agent":aid,"step":step,"status":r.status.value,"score":r.quality_score})
            return r

        results = await asyncio.gather(*[_run(a,s) for a,s in self.WORKERS], return_exceptions=False)
        all_arts, failures, min_score = [], [], 1.0
        for (aid,step), r in zip(self.WORKERS, results):
            all_arts.extend(r.artifacts); min_score = min(min_score, r.quality_score)
            _ctx_update(task, r)
            if r.status in (TaskStatus.FAILED, TaskStatus.ESCALATED):
                failures.append(f"{step}: {r.failure_reason}")
        if failures: log.warning("platform_partial_failures", failures=failures)
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"platform_design":"complete","failures":failures},
            summary=f"Platform Design: {len(self.WORKERS)} parallel, {len(failures)} failures, {len(all_arts)} artifacts",
            quality_score=min_score, artifacts=all_arts)


@AgentFactory.register("architecture_review_lead")
class ReviewLead(BaseAgent):
    """Sequential: traceability (→ req_dependencies table) → architecture review."""
    COVERAGE_THRESHOLD = 0.80

    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        all_arts = []

        # Step 1: Traceability
        log.info("review_lead_traceability", project_id=task.project_id)
        if factory:
            tr = await factory.create("traceability_agent_worker").run(task)
        else:
            tr = AgentResult(task_id=task.task_id, agent_id="traceability_agent_worker",
                status=TaskStatus.COMPLETED, quality_score=0.9,
                content={"coverage_summary":{"coverage_percentage":100.0},"traceability_matrix":[]},
                summary="Traceability placeholder")
        all_arts.extend(tr.artifacts); _ctx_update(task, tr)

        coverage_pct = (tr.content or {}).get("coverage_summary",{}).get("coverage_percentage",0.0) / 100.0
        if coverage_pct < self.COVERAGE_THRESHOLD:
            uncovered = (tr.content or {}).get("uncovered_requirements",[])
            return self.escalate(task,
                f"Coverage {coverage_pct*100:.1f}% < 80%. Uncovered: {uncovered[:5]}. Re-run system design.")

        # Step 2: Architecture review
        log.info("review_lead_arch_review", project_id=task.project_id)
        if factory:
            rv = await factory.create("architecture_reviewer_worker").run(task)
        else:
            rv = AgentResult(task_id=task.task_id, agent_id="architecture_reviewer_worker",
                status=TaskStatus.COMPLETED, quality_score=0.9,
                content={"overall_passed":True,"overall_score":0.9}, summary="Review placeholder")
        all_arts.extend(rv.artifacts); _ctx_update(task, rv)

        if rv.status == TaskStatus.FAILED:
            return self.escalate(task, f"Architecture review failed: {rv.failure_reason}")

        await self.notify_ui(task.project_id,"review_completed",
            {"coverage_pct":coverage_pct*100,"review_passed":rv.content.get("overall_passed",False)})
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"review_complete":True,"coverage_pct":coverage_pct*100,
                     "review_passed":rv.content.get("overall_passed",False)},
            summary=f"Review Lead: coverage={coverage_pct*100:.1f}%, review={'PASSED' if rv.content.get('overall_passed') else 'FAILED'}",
            quality_score=(tr.quality_score+rv.quality_score)/2, artifacts=all_arts)
