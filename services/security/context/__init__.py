"""
services/security/context — task decomposition, dependency scheduling,
and the deterministic validation-gate logic that plays the role of the
spec's "Risk Lead" (Risk Classification / Security Report / Retry
Coordinator).

Design decision (see docs/M3.5_Security_Service_Handover.md, "Department
Structure"): the platform-wide AgentRegistry (core/runtime/factory.py)
already reserves exactly 9 agent_ids for the `security` department — 1
head, 3 leads (dependency_scan/code_security/compliance), and 5
workers (cve_scanner, owasp_checker, secret_scanner, injection_check,
compliance_validator). The spec's org chart proposes a different shape
(Static Analysis Lead / Compliance Lead / Risk Lead, each with 3
workers). Renaming or expanding AGENT_REGISTRY is out of scope per the
M3.5 constraints ("Do not modify ... AgentFactory"), so Security is
implemented against the already-registered 9 agents:

  Spec responsibility                       Where it lives
  -----------------------------------------  --------------------------------
  Dependency Scan (CVE)                      cve_scanner_worker (dependency_scan_lead)
  Static Analysis (Code Scan)                owasp_checker_worker (code_security_lead)
  Static Analysis (injection patterns)       injection_check_worker (code_security_lead)
  Secret Scan                                secret_scanner_worker (code_security_lead)
  Compliance / License / SBOM                compliance_validator_worker (compliance_lead)
  Risk Classification / Security Report /    Deterministic functions in this
    Retry Coordinator ("Risk Lead")           module, invoked by SecurityHead —
                                               exactly how QA's Reporting Lead
                                               (classify_defects/build_qa_report/
                                               build_retry_request) is plain
                                               Python rather than separate agents.

This keeps every required *output* (SecurityReport, SecurityFinding,
RetryRequest, DependencyScan/StaticAnalysisReport/SecretScan/SBOM/
LicenseReport/ComplianceReport/RiskAssessment) intact without inventing
unregistered agent_ids.
"""
from __future__ import annotations

from typing import Any, Dict, List, Set

from services.security.models import (
    ComplianceReport,
    DependencyScan,
    FindingCategory,
    FindingSeverity,
    LicenseReport,
    RetryRequest,
    RiskAssessment,
    ScanTeam,
    SecretScan,
    SecurityFinding,
    SecurityPlan,
    SecurityReport,
    SecurityTask,
    SecurityVerdict,
    StaticAnalysisReport,
)
from services.security.utils import finding_id_for, severity_for_category

# Worker assignment per team, matching AGENT_REGISTRY's security-service entries.
DEPENDENCY_WORKERS = ["cve_scanner_worker"]
CODE_WORKERS       = ["owasp_checker_worker", "secret_scanner_worker", "injection_check_worker"]
COMPLIANCE_WORKERS = ["compliance_validator_worker"]

_DEPENDENCY_DEPS: Dict[str, List[str]] = {"cve_scanner_worker": []}
_CODE_DEPS: Dict[str, List[str]] = {
    "owasp_checker_worker": [], "secret_scanner_worker": [], "injection_check_worker": [],
}
_COMPLIANCE_DEPS: Dict[str, List[str]] = {"compliance_validator_worker": []}

DEFAULT_RISK_THRESHOLD = 70.0   # risk_score >= this is "high" or above


def build_security_plan(
    project_id: str,
    feature_name: str,
    engineering_refs: Dict[str, Any],
) -> SecurityPlan:
    """
    Receive Engineering -> Validate Inputs -> the task graph consumed by
    the Static Analysis / Parallel Fan-Out stages. Dependency, Code, and
    Compliance teams all run in parallel — Security validates every
    Engineering deliverable the same way QA does, just concurrently
    with QA rather than after it.
    """
    plan = SecurityPlan(project_id=project_id, feature_name=feature_name, engineering_refs=engineering_refs)
    id_by_worker: Dict[str, str] = {}

    def _add(team: ScanTeam, worker_id: str, deps_worker_ids: List[str]) -> None:
        task = SecurityTask(
            project_id=project_id,
            team=team,
            worker_agent_id=worker_id,
            description=f"{team.value}:{worker_id} for feature '{feature_name}'",
            depends_on=[id_by_worker[d] for d in deps_worker_ids if d in id_by_worker],
        )
        id_by_worker[worker_id] = task.task_id
        plan.tasks.append(task)

    for w in DEPENDENCY_WORKERS:
        _add(ScanTeam.DEPENDENCY, w, _DEPENDENCY_DEPS.get(w, []))
    for w in CODE_WORKERS:
        _add(ScanTeam.CODE, w, _CODE_DEPS.get(w, []))
    for w in COMPLIANCE_WORKERS:
        _add(ScanTeam.COMPLIANCE, w, _COMPLIANCE_DEPS.get(w, []))

    return plan


def topological_batches(tasks: List[SecurityTask]) -> List[List[SecurityTask]]:
    """Splits tasks into dependency-ordered batches. Raises ValueError on a cycle."""
    remaining = {t.task_id: t for t in tasks}
    done: Set[str] = set()
    batches: List[List[SecurityTask]] = []

    while remaining:
        batch = [t for t in remaining.values() if all(d in done for d in t.depends_on)]
        if not batch:
            raise ValueError(f"Dependency cycle detected among Security tasks: {list(remaining.keys())}")
        batches.append(batch)
        for t in batch:
            done.add(t.task_id)
            del remaining[t.task_id]

    return batches


def team_progress(plan: SecurityPlan, team: ScanTeam) -> Dict[str, int]:
    team_tasks = plan.tasks_by_team(team)
    return {
        "total":     len(team_tasks),
        "completed": sum(1 for t in team_tasks if t.status.value == "completed"),
        "failed":    sum(1 for t in team_tasks if t.status.value == "failed"),
        "escalated": sum(1 for t in team_tasks if t.escalated),
    }


# ── Deterministic validation-gate logic ("Risk Lead") ──────────

def classify_findings(
    project_id: str,
    dependency_scan: DependencyScan,
    static_analysis: StaticAnalysisReport,
    secret_scan: SecretScan,
    license_report: LicenseReport,
    compliance_report: ComplianceReport,
) -> List[SecurityFinding]:
    """
    Applies the spec's Hard Fail / Warning condition rules and emits one
    SecurityFinding per violated condition. Never mutates code — Security
    only ever produces structured finding data for Engineering to act on.
    """
    findings: List[SecurityFinding] = []

    def _add(category: FindingCategory, description: str, module_id: str = None,
              severity_override: FindingSeverity = None) -> None:
        severity = severity_override or FindingSeverity(severity_for_category(category.value))
        findings.append(SecurityFinding(
            finding_id=finding_id_for(project_id, category.value, module_id or description[:32]),
            project_id=project_id, category=category, severity=severity,
            description=description, source_worker="security_head", module_id=module_id,
        ))

    for v in dependency_scan.vulnerabilities:
        _add(FindingCategory.CVE, f"{v.cve_id} in {v.package}@{v.version}: {v.description}",
             module_id=v.package, severity_override=v.severity)

    for issue in static_analysis.all_findings:
        _add(FindingCategory.OWASP if issue in static_analysis.owasp_findings else FindingCategory.INJECTION,
             f"[{issue.rule}] {issue.description} ({issue.file})",
             module_id=issue.file, severity_override=issue.severity)

    for hit in secret_scan.secrets:
        _add(FindingCategory.SECRET, f"Potential secret ({hit.rule}) in {hit.file}:{hit.line}",
             module_id=hit.file, severity_override=hit.severity)

    if not license_report.compliant:
        _add(FindingCategory.LICENSE,
             f"Disallowed license(s) detected: {license_report.disallowed_licenses}")

    if not compliance_report.compliant:
        _add(FindingCategory.COMPLIANCE_VIOLATION,
             f"Compliance violation(s): {compliance_report.violations}")

    return findings


def build_risk_assessment(
    project_id: str,
    findings: List[SecurityFinding],
) -> RiskAssessment:
    """
    Deterministic risk score: weighted sum of finding severities, capped
    at 100. critical=40, high=20, medium=8, low=2 per finding.
    """
    weights = {
        FindingSeverity.CRITICAL: 40.0,
        FindingSeverity.HIGH:     20.0,
        FindingSeverity.MEDIUM:   8.0,
        FindingSeverity.LOW:      2.0,
    }
    score = min(100.0, sum(weights[f.severity] for f in findings))

    if score >= 80.0:
        level = "critical"
    elif score >= DEFAULT_RISK_THRESHOLD:
        level = "high"
    elif score >= 30.0:
        level = "medium"
    else:
        level = "low"

    factors = [f"{f.category.value}:{f.severity.value}" for f in findings if f.is_blocking]
    return RiskAssessment(
        project_id=project_id, risk_score=score, risk_level=level,
        contributing_factors=factors,
    )


def build_security_report(
    project_id: str,
    findings: List[SecurityFinding],
    risk: RiskAssessment,
) -> SecurityReport:
    """
    Aggregates all validation outcomes into the final SecurityReport
    verdict, per the spec's Hard Fail / Warning rules:
      FAIL — any hard-fail finding (critical vulnerability, secret
             detected, critical CVE, compliance violation, high-risk
             dependency)
      WARN — only non-blocking issues (medium CVE, deprecated
             dependency, license warning, low severity issue)
      PASS — no findings at all
    """
    blocking = [f.description for f in findings if f.is_blocking]
    warnings = [f.description for f in findings if not f.is_blocking]

    if blocking:
        verdict = SecurityVerdict.FAIL
    elif warnings:
        verdict = SecurityVerdict.WARN
    else:
        verdict = SecurityVerdict.PASS

    return SecurityReport(
        project_id=project_id, verdict=verdict,
        blocking_conditions=blocking, warning_conditions=warnings,
        finding_ids=[f.finding_id for f in findings],
        risk_level=risk.risk_level, risk_score=risk.risk_score,
        retry_requested=bool(blocking),
    )


def build_retry_request(project_id: str, target_team: str, reason: str, retry_count: int = 0) -> RetryRequest:
    return RetryRequest(project_id=project_id, target_team=target_team, reason=reason, retry_count=retry_count)
