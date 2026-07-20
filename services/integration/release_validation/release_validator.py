"""
services/integration/release_validation/release_validator.py
=================================
M4.10 §1 Release Validation — the "release score" aggregator. Takes the
outputs of `dependency_checker`, `compatibility_matrix`,
`environment_audit`, `benchmark_summary`, and a documentation-
completeness scan, and folds them into one `ReleaseScore` (§1: "Generate
release score... Produce one unified Release Report" — the unification
itself lives in `readiness_report.py`; this module owns only the scoring
arithmetic so it can be unit-tested against synthetic sub-reports
without needing a real filesystem/docs tree).
"""
from __future__ import annotations

from typing import List, Optional

from services.integration.release_validation.release_validation_models import (
    CheckStatus, CompatibilityMatrix, DependencyReport, DocumentationCompleteness, EnvironmentAuditReport,
    ReleaseCheckItem, ReleaseScore,
)


def _dependency_check(report: DependencyReport) -> ReleaseCheckItem:
    status = CheckStatus.PASS if report.clean else CheckStatus.FAIL
    detail = f"{report.total_dependencies} dependencies, {len(report.issues)} issue(s)"
    return ReleaseCheckItem(name="dependency_report", category="dependency", status=status, detail=detail, weight=2.0)


def _compatibility_check(matrix: CompatibilityMatrix) -> ReleaseCheckItem:
    if matrix.all_compatible and matrix.undetermined_count == 0:
        status = CheckStatus.PASS
    elif matrix.all_compatible:
        status = CheckStatus.WARN
    else:
        status = CheckStatus.FAIL
    detail = f"{len(matrix.entries)} components checked, {matrix.undetermined_count} undetermined"
    return ReleaseCheckItem(name="compatibility_matrix", category="compatibility", status=status, detail=detail,
                             weight=2.0)


def _environment_check(audit: EnvironmentAuditReport) -> ReleaseCheckItem:
    status_map = {CheckStatus.PASS: CheckStatus.PASS, CheckStatus.SKIPPED: CheckStatus.WARN,
                  CheckStatus.WARN: CheckStatus.WARN, CheckStatus.FAIL: CheckStatus.FAIL}
    status = status_map[audit.overall_status]
    detail = f"{len(audit.items)} checks, overall={audit.overall_status.value}"
    return ReleaseCheckItem(name="environment_audit", category="environment", status=status, detail=detail,
                             weight=2.0)


def _documentation_check(doc: DocumentationCompleteness) -> ReleaseCheckItem:
    if doc.completeness_pct == 100.0:
        status = CheckStatus.PASS
    elif doc.completeness_pct >= 70.0:
        status = CheckStatus.WARN
    else:
        status = CheckStatus.FAIL
    detail = f"{doc.completeness_pct}% complete, missing: {doc.missing_documents or 'none'}"
    return ReleaseCheckItem(name="documentation_completeness", category="documentation", status=status,
                             detail=detail, weight=1.5)


def _benchmark_check(regressions: List[str]) -> ReleaseCheckItem:
    status = CheckStatus.PASS if not regressions else CheckStatus.WARN
    detail = "no regressions" if not regressions else f"regressions in: {regressions}"
    return ReleaseCheckItem(name="benchmark_summary", category="benchmark", status=status, detail=detail, weight=1.0)


_STATUS_CREDIT = {CheckStatus.PASS: 1.0, CheckStatus.WARN: 0.5, CheckStatus.FAIL: 0.0, CheckStatus.SKIPPED: 0.0}


def compute_release_score(dependency_report: DependencyReport, compatibility_matrix: CompatibilityMatrix,
                           environment_audit: EnvironmentAuditReport, documentation: DocumentationCompleteness,
                           benchmark_regressions: Optional[List[str]] = None,
                           extra_checks: Optional[List[ReleaseCheckItem]] = None) -> ReleaseScore:
    checks = [
        _dependency_check(dependency_report),
        _compatibility_check(compatibility_matrix),
        _environment_check(environment_audit),
        _documentation_check(documentation),
        _benchmark_check(benchmark_regressions or []),
    ]
    checks.extend(extra_checks or [])
    total_weight = sum(c.weight for c in checks)
    earned = sum(c.weight * _STATUS_CREDIT[c.status] for c in checks)
    return ReleaseScore(score=round(earned, 2), max_score=round(total_weight, 2), checks=checks)
