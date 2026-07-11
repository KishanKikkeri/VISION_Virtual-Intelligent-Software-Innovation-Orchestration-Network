"""
services/qa/head — L3 QAHead: orchestrates the full M3.4 pipeline.

Pipeline (matches the LangGraph in workflows/qa_graph.py and the spec's
Mission: QA validates, it never generates software):
  Stage 1: Receive Engineering Artifacts — read approved Engineering output
  Stage 2: Validate Inputs               — refuse to run without them
  Stage 3: Generate Test Suites          — Unit / Integration leads (parallel)
  Stage 4: Parallel Test Execution       — Regression / Performance leads (parallel)
  Stage 5: Coverage Analysis + Aggregate — deterministic gate (services.qa.context)
  Stage 6: PASS -> Publish / FAIL -> Defect Report + Retry Request
"""
from __future__ import annotations

import asyncio
from typing import Optional

import structlog

from core.contracts import AgentResult, NATSEvent, TaskStatus, WebSocketEvent
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.qa.context import DEFAULT_COVERAGE_THRESHOLD, build_qa_plan, build_qa_report, build_retry_request, classify_defects
from services.qa.models import CompatibilityReport, CoverageReport, PerformanceReport, QAVerdict, RegressionReport

log = structlog.get_logger(__name__)

GENERATION_LEADS = [
    ("unit_test_lead", "Unit"),
    ("integration_test_lead", "Integration"),
]
EXECUTION_LEADS = [
    ("regression_test_lead", "Regression"),
    ("performance_test_lead", "Performance"),
]

# Inputs QA must never regenerate or modify — presence is validated at Stage 2.
REQUIRED_ENGINEERING_ARTIFACTS = ("source_code",)


def _artifact_type_of(a) -> Optional[str]:
    if isinstance(a, dict):
        return a.get("artifact_type")
    return getattr(a, "artifact_type", None)


def _ctx_update(task, result):
    if result.content and isinstance(result.content, dict):
        for a in result.artifacts:
            t = _artifact_type_of(a)
            if t:
                task.context.approved_artifacts[t] = result.content


@AgentFactory.register("qa_head")
class QAHead(BaseAgent):
    """L3 — Sole orchestrator of qa-service."""

    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        feature_name = task.context.approved_artifacts.get("__feature_name__", "default")

        await self.notify_ui(task.project_id, "phase_started", {
            "phase": 5, "phase_name": "QA Validation",
            "message": "QA pipeline starting",
        })
        await self.publish_event("qa.phase.started",
            {"project_id": task.project_id, "feature_name": feature_name})

        # ── Stage 2: Validate Inputs ──────────────────────────────
        missing = [a for a in REQUIRED_ENGINEERING_ARTIFACTS if not task.context.get_artifact(a)]
        if missing:
            reason = f"Missing required Engineering artifact(s): {missing}"
            await self.publish_event("qa.phase.failed", {"project_id": task.project_id, "reason": reason})
            return self.escalate(task, reason)

        plan = build_qa_plan(
            project_id=task.project_id, feature_name=feature_name,
            engineering_refs=task.context.approved_artifacts,
        )
        task.context.approved_artifacts["__qa_plan__"] = plan.model_dump()

        # ── Stage 3: Generate Test Suites (Unit + Integration, parallel) ──
        async def _run_lead(agent_id: str, step: str) -> AgentResult:
            if not factory:
                return AgentResult(task_id=task.task_id, agent_id=agent_id, status=TaskStatus.COMPLETED,
                                    content={"placeholder": True}, summary=f"{step} placeholder", quality_score=0.8)
            r = await factory.create(agent_id).run(task)
            await self.notify_ui(task.project_id, "agent_completed",
                {"agent": agent_id, "step": step, "status": r.status.value, "score": r.quality_score})
            return r

        gen_results = await asyncio.gather(*[_run_lead(a, s) for a, s in GENERATION_LEADS])
        all_artifacts = []
        for (agent_id, step), r in zip(GENERATION_LEADS, gen_results):
            all_artifacts.extend(r.artifacts)
            _ctx_update(task, r)

        # ── Stage 4: Parallel Test Execution (Regression + Performance) ──
        exec_results = await asyncio.gather(*[_run_lead(a, s) for a, s in EXECUTION_LEADS])
        for (agent_id, step), r in zip(EXECUTION_LEADS, exec_results):
            all_artifacts.extend(r.artifacts)
            _ctx_update(task, r)

        # ── Stage 5: Coverage Analysis + Aggregate Results ────────
        coverage_data = task.context.get_artifact("coverage_report", {})
        coverage = CoverageReport(**coverage_data) if coverage_data else CoverageReport.build(
            task.project_id, 0.0, DEFAULT_COVERAGE_THRESHOLD)

        regression_data = task.context.get_artifact("regression_report", {})
        regression = RegressionReport(**regression_data) if regression_data else RegressionReport(project_id=task.project_id)

        performance_data = task.context.get_artifact("performance_report", {})
        performance = PerformanceReport(**performance_data) if performance_data else PerformanceReport(project_id=task.project_id)

        compatibility = CompatibilityReport(project_id=task.project_id)

        contract_valid = task.context.approved_artifacts.get(
            "integration_test_writer_worker", {}).get("contract_valid", True)
        build_succeeded = task.context.approved_artifacts.get("__build_succeeded__", True)
        migration_succeeded = task.context.approved_artifacts.get("__migration_succeeded__", True)

        defects = classify_defects(
            project_id=task.project_id,
            build_succeeded=build_succeeded, migration_succeeded=migration_succeeded,
            contract_valid=contract_valid, coverage=coverage, regression=regression,
            performance=performance, compatibility=compatibility,
        )

        unit_tests = task.context.get_artifact("unit_test_suite", {})
        integration_tests = task.context.get_artifact("integration_test_suite", {})
        tests_total = (unit_tests.get("test_count", 0) if isinstance(unit_tests, dict) else 0) + (
            integration_tests.get("test_count", 0) if isinstance(integration_tests, dict) else 0)
        tests_failed = regression.tests_failed
        tests_passed = max(0, tests_total - tests_failed)

        qa_report = build_qa_report(
            project_id=task.project_id, build_succeeded=build_succeeded,
            migration_succeeded=migration_succeeded, contract_valid=contract_valid,
            coverage=coverage, regression=regression, performance=performance,
            compatibility=compatibility, tests_total=tests_total,
            tests_passed=tests_passed, tests_failed=tests_failed, defects=defects,
        )

        for d in defects:
            await self.publish_event("qa.defect.created", d.model_dump())
        report_artifact = await self.create_artifact(task, "qa_report", qa_report.model_dump())
        all_artifacts.append(report_artifact)

        # ── Stage 6: PASS -> Publish / FAIL -> Defect Report + Retry Request ──
        passed = qa_report.verdict != QAVerdict.FAIL
        retry_request = None
        if qa_report.retry_requested:
            retry_request = build_retry_request(
                task.project_id, target_team="engineering",
                reason="; ".join(qa_report.blocking_conditions) or "QA gate failed",
            )
            await self.publish_event("qa.retry.requested", retry_request.model_dump())

        await self.write_memory(
            task, f"QA {'PASSED' if passed else 'FAILED'} for {task.project_id}: "
                  f"coverage={coverage.line_coverage:.1f}%, defects={len(defects)}",
            source="qa_head",
        )

        completed_event = {
            "project_id": task.project_id, "workflow_id": task.context.workflow_id,
            "feature_name": feature_name, "passed": passed,
            "coverage_pct": coverage.line_coverage, "tests_total": tests_total,
            "defect_count": len(defects),
        }
        subject = "qa.phase.completed" if passed else "qa.phase.failed"
        await self.publish_event(subject, completed_event)

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id,
            status=TaskStatus.COMPLETED if passed else TaskStatus.FAILED,
            content={"phase": "qa", "status": "complete" if passed else "failed",
                     "verdict": qa_report.verdict.value, "plan_id": plan.plan_id,
                     "qa_report": qa_report.model_dump(),
                     "defects": [d.model_dump() for d in defects],
                     "retry_request": retry_request.model_dump() if retry_request else None},
            summary=f"QA {qa_report.verdict.value.upper()}: {tests_total} tests, "
                    f"{coverage.line_coverage:.1f}% coverage, {len(defects)} defect(s)",
            quality_score=1.0 if passed else 0.0,
            artifacts=all_artifacts,
            nats_events=[NATSEvent(subject=subject, payload=completed_event, project_id=task.project_id)],
            ws_events=[WebSocketEvent(project_id=task.project_id, event_type="phase_completed" if passed else "phase_failed",
                payload={"phase": 5, "phase_name": "QA Validation",
                         "message": "QA passed — ready for Security/DevOps" if passed
                         else "QA failed — defects routed to Engineering"})],
            failure_reason=None if passed else "; ".join(qa_report.blocking_conditions),
        )
