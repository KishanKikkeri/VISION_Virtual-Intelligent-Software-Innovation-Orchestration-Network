"""
services/security/head — L3 SecurityHead: orchestrates the full M3.5 pipeline.

Pipeline (matches the LangGraph in workflows/security_graph.py and the
spec's Mission: Security validates in parallel with QA, it never
generates or edits software):
  Stage 1: Receive Engineering Artifacts — read approved Engineering output
  Stage 2: Validate Inputs               — refuse to run without them
  Stage 3: Static Analysis + Parallel Fan-Out — Dependency / Code / Compliance
           leads, all concurrent (Code Security Lead's OWASP + Injection +
           Secret workers ARE the spec's "Static Analysis" + "Secret Scan"
           steps — see docs/M3.5 handover's Department Structure note)
  Stage 4: Risk Classification + Aggregate  — deterministic gate (services.security.context)
  Stage 5: PASS/WARN -> Publish / FAIL -> Security Finding(s) + Retry Request
"""
from __future__ import annotations

import asyncio
from typing import Optional

import structlog

from core.contracts import AgentResult, NATSEvent, TaskStatus, WebSocketEvent
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.security.context import (
    build_retry_request,
    build_risk_assessment,
    build_security_plan,
    build_security_report,
    classify_findings,
)
from services.security.models import (
    ComplianceReport,
    DependencyScan,
    LicenseReport,
    SecretScan,
    SecurityVerdict,
    StaticAnalysisReport,
)

log = structlog.get_logger(__name__)

SCAN_LEADS = [
    ("dependency_scan_lead", "Dependency Scan"),
    ("code_security_lead", "Code Security"),
    ("compliance_lead", "Compliance"),
]

# Inputs Security must never regenerate or modify — presence is validated
# at Stage 2. Per the spec, Security also consumes Repository/Commit
# Metadata, Dependency Manifest, OpenAPI Spec, Database Schema,
# Deployment Plan, and Container Configuration — but only `source_code`
# is treated as hard-required. The rest (especially Deployment Plan and
# Container Configuration, which are DevOps/M3.6 outputs that don't
# exist yet) are consumed best-effort when present, exactly the way QA
# treats `openapi_spec` as optional context rather than a hard gate.
# Documented deviation — see docs/M3.5_Security_Service_Handover.md.
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
                task.context.approved_artifacts.setdefault(t, result.content)


@AgentFactory.register("security_head")
class SecurityHead(BaseAgent):
    """L3 — Sole orchestrator of security-service."""

    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        feature_name = task.context.approved_artifacts.get("__feature_name__", "default")

        await self.notify_ui(task.project_id, "phase_started", {
            "phase": 6, "phase_name": "Security Validation",
            "message": "Security pipeline starting (parallel with QA)",
        })
        await self.publish_event("security.phase.started",
            {"project_id": task.project_id, "feature_name": feature_name})

        # ── Stage 2: Validate Inputs ──────────────────────────────
        missing = [a for a in REQUIRED_ENGINEERING_ARTIFACTS if not task.context.get_artifact(a)]
        if missing:
            reason = f"Missing required Engineering artifact(s): {missing}"
            await self.publish_event("security.phase.failed", {"project_id": task.project_id, "reason": reason})
            return self.escalate(task, reason)

        plan = build_security_plan(
            project_id=task.project_id, feature_name=feature_name,
            engineering_refs=task.context.approved_artifacts,
        )
        task.context.approved_artifacts["__security_plan__"] = plan.model_dump()

        # ── Stage 3: Static Analysis + Parallel Fan-Out ───────────
        async def _run_lead(agent_id: str, step: str) -> AgentResult:
            if not factory:
                return AgentResult(task_id=task.task_id, agent_id=agent_id, status=TaskStatus.COMPLETED,
                                    content={"placeholder": True}, summary=f"{step} placeholder", quality_score=0.8)
            r = await factory.create(agent_id).run(task)
            await self.notify_ui(task.project_id, "agent_completed",
                {"agent": agent_id, "step": step, "status": r.status.value, "score": r.quality_score})
            return r

        scan_results = await asyncio.gather(*[_run_lead(a, s) for a, s in SCAN_LEADS])
        all_artifacts = []
        for (agent_id, step), r in zip(SCAN_LEADS, scan_results):
            all_artifacts.extend(r.artifacts)
            _ctx_update(task, r)

        # ── Stage 4: Risk Classification + Aggregate Results ──────
        dep_data = task.context.approved_artifacts.get("cve_scanner_worker", {})
        dependency_scan = DependencyScan(**dep_data) if dep_data else DependencyScan(project_id=task.project_id)

        owasp_data = task.context.approved_artifacts.get("owasp_checker_worker", {})
        injection_data = task.context.approved_artifacts.get("injection_check_worker", {})
        static_analysis = StaticAnalysisReport(
            project_id=task.project_id,
            files_scanned=max(owasp_data.get("files_scanned", 0), injection_data.get("files_scanned", 0)),
            owasp_findings=owasp_data.get("owasp_findings", []),
            injection_findings=injection_data.get("injection_findings", []),
        )

        secret_data = task.context.approved_artifacts.get("secret_scanner_worker", {})
        secret_scan = SecretScan(**secret_data) if secret_data else SecretScan(project_id=task.project_id)

        compliance_data = task.context.approved_artifacts.get("compliance_validator_worker", {})
        license_report = LicenseReport(**compliance_data.get("license_report", {"project_id": task.project_id})) \
            if compliance_data else LicenseReport(project_id=task.project_id)
        compliance_report = ComplianceReport(**compliance_data.get("compliance_report", {"project_id": task.project_id})) \
            if compliance_data else ComplianceReport(project_id=task.project_id)

        findings = classify_findings(
            project_id=task.project_id, dependency_scan=dependency_scan,
            static_analysis=static_analysis, secret_scan=secret_scan,
            license_report=license_report, compliance_report=compliance_report,
        )
        risk = build_risk_assessment(task.project_id, findings)
        security_report = build_security_report(task.project_id, findings, risk)

        for f in findings:
            await self.publish_event("security.findings.created", f.model_dump())
        risk_artifact = await self.create_artifact(task, "risk_assessment", risk.model_dump())
        report_artifact = await self.create_artifact(task, "security_report", security_report.model_dump())
        all_artifacts.extend([risk_artifact, report_artifact])

        # ── Stage 5: PASS/WARN -> Publish / FAIL -> Retry Request ──
        passed = security_report.verdict != SecurityVerdict.FAIL
        retry_request = None
        if security_report.retry_requested:
            retry_request = build_retry_request(
                task.project_id, target_team="engineering",
                reason="; ".join(security_report.blocking_conditions) or "Security gate failed",
            )
            await self.publish_event("security.retry.requested", retry_request.model_dump())

        await self.write_memory(
            task, f"Security {'PASSED' if passed else 'FAILED'} for {task.project_id}: "
                  f"risk={risk.risk_level}({risk.risk_score:.0f}), findings={len(findings)}",
            source="security_head",
        )

        completed_event = {
            "project_id": task.project_id, "workflow_id": task.context.workflow_id,
            "feature_name": feature_name, "passed": passed,
            "risk_level": risk.risk_level, "risk_score": risk.risk_score,
            "finding_count": len(findings),
        }
        subject = "security.phase.completed" if passed else "security.phase.failed"
        await self.publish_event(subject, completed_event)

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id,
            status=TaskStatus.COMPLETED if passed else TaskStatus.FAILED,
            content={"phase": "security", "status": "complete" if passed else "failed",
                     "verdict": security_report.verdict.value, "plan_id": plan.plan_id,
                     "security_report": security_report.model_dump(),
                     "risk_assessment": risk.model_dump(),
                     "findings": [f.model_dump() for f in findings],
                     "retry_request": retry_request.model_dump() if retry_request else None},
            summary=f"Security {security_report.verdict.value.upper()}: {len(findings)} finding(s), "
                    f"risk={risk.risk_level}({risk.risk_score:.0f})",
            quality_score=1.0 if passed else 0.0,
            artifacts=all_artifacts,
            nats_events=[NATSEvent(subject=subject, payload=completed_event, project_id=task.project_id)],
            ws_events=[WebSocketEvent(project_id=task.project_id, event_type="phase_completed" if passed else "phase_failed",
                payload={"phase": 6, "phase_name": "Security Validation",
                         "message": "Security passed — ready for Manager/DevOps" if passed
                         else "Security failed — findings routed to Engineering"})],
            failure_reason=None if passed else "; ".join(security_report.blocking_conditions),
        )
