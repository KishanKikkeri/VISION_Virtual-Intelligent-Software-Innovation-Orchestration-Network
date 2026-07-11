"""
services/qa/context — task decomposition, dependency scheduling, and the
deterministic validation-gate logic that plays the role of the spec's
"Reporting Lead" (QA Report / Defect Classification / Retry Coordinator).

Design decision (see docs/M3.4_QA_Service_Handover.md, "Department
Structure"): the platform-wide AgentRegistry (core/runtime/factory.py)
already reserves exactly 10 agent_ids for the `qa` department — 1 head,
4 leads (unit/integration/regression/performance), and 5 workers. The
spec's org chart proposes a different shape (3 leads x 4 workers).
Renaming or expanding AGENT_REGISTRY is out of scope per the M3.4
constraints ("Do not modify ... AgentFactory"), so QA is implemented
against the already-registered 10 agents. Coverage analysis, defect
classification, and retry-request generation — the spec's "Reporting
Lead" responsibilities — are implemented here as deterministic
functions invoked by QAHead, exactly the way Engineering's health
scoring and coding-contract checks are plain Python rather than
separate agents. This keeps every required *output* (QAReport,
DefectReport, RetryRequest, coverage/perf/regression reports) intact
without inventing unregistered agent_ids.
"""
from __future__ import annotations

from typing import Any, Dict, List, Set

from services.qa.models import (
    CompatibilityReport,
    CoverageReport,
    DefectReport,
    DefectSeverity,
    FailureCategory,
    PerformanceReport,
    QAPlan,
    QAReport,
    QATask,
    QATeam,
    QAVerdict,
    RegressionReport,
    RetryRequest,
)
from services.qa.utils import defect_id_for, severity_for_category

# Worker assignment per team, matching AGENT_REGISTRY's qa-service entries.
UNIT_WORKERS        = ["unit_test_writer_worker", "coverage_analyzer_worker"]
INTEGRATION_WORKERS = ["integration_test_writer_worker"]
REGRESSION_WORKERS  = ["regression_suite_worker"]
PERFORMANCE_WORKERS = ["performance_test_worker"]

_UNIT_DEPS        = {"unit_test_writer_worker": [], "coverage_analyzer_worker": ["unit_test_writer_worker"]}
_INTEGRATION_DEPS: Dict[str, List[str]] = {"integration_test_writer_worker": []}
_REGRESSION_DEPS:  Dict[str, List[str]] = {"regression_suite_worker": []}
_PERFORMANCE_DEPS: Dict[str, List[str]] = {"performance_test_worker": []}

DEFAULT_COVERAGE_THRESHOLD = 80.0


def build_qa_plan(
    project_id: str,
    feature_name: str,
    engineering_refs: Dict[str, Any],
) -> QAPlan:
    """
    Stage 1 -> Stage 2 of the QA graph: "Receive Engineering Artifacts" ->
    "Validate Inputs" -> the task graph consumed by "Generate Test Suites".
    Unit, Integration, Regression, and Performance tasks all run — QA
    validates every Engineering deliverable regardless of which teams
    produced it (unlike Engineering's Frontend team, which is
    conditional on ui_blueprint).
    """
    plan = QAPlan(project_id=project_id, feature_name=feature_name, engineering_refs=engineering_refs)
    id_by_worker: Dict[str, str] = {}

    def _add(team: QATeam, worker_id: str, deps_worker_ids: List[str]) -> None:
        task = QATask(
            project_id=project_id,
            team=team,
            worker_agent_id=worker_id,
            description=f"{team.value}:{worker_id} for feature '{feature_name}'",
            depends_on=[id_by_worker[d] for d in deps_worker_ids if d in id_by_worker],
        )
        id_by_worker[worker_id] = task.task_id
        plan.tasks.append(task)

    for w in UNIT_WORKERS:
        _add(QATeam.UNIT, w, _UNIT_DEPS.get(w, []))
    for w in INTEGRATION_WORKERS:
        _add(QATeam.INTEGRATION, w, _INTEGRATION_DEPS.get(w, []))
    for w in REGRESSION_WORKERS:
        _add(QATeam.REGRESSION, w, _REGRESSION_DEPS.get(w, []))
    for w in PERFORMANCE_WORKERS:
        _add(QATeam.PERFORMANCE, w, _PERFORMANCE_DEPS.get(w, []))

    return plan


def topological_batches(tasks: List[QATask]) -> List[List[QATask]]:
    """Splits tasks into dependency-ordered batches. Raises ValueError on a cycle."""
    remaining = {t.task_id: t for t in tasks}
    done: Set[str] = set()
    batches: List[List[QATask]] = []

    while remaining:
        batch = [t for t in remaining.values() if all(d in done for d in t.depends_on)]
        if not batch:
            raise ValueError(f"Dependency cycle detected among QA tasks: {list(remaining.keys())}")
        batches.append(batch)
        for t in batch:
            done.add(t.task_id)
            del remaining[t.task_id]

    return batches


def team_progress(plan: QAPlan, team: QATeam) -> Dict[str, int]:
    team_tasks = plan.tasks_by_team(team)
    return {
        "total":     len(team_tasks),
        "completed": sum(1 for t in team_tasks if t.status.value == "completed"),
        "failed":    sum(1 for t in team_tasks if t.status.value == "failed"),
        "escalated": sum(1 for t in team_tasks if t.escalated),
    }


# ── Deterministic validation-gate logic ("Reporting Lead") ────

def classify_defects(
    project_id: str,
    build_succeeded: bool,
    migration_succeeded: bool,
    contract_valid: bool,
    coverage: CoverageReport,
    regression: RegressionReport,
    performance: PerformanceReport,
    compatibility: CompatibilityReport,
    module_id: str = None,
    commit_sha: str = None,
    pull_request_id: str = None,
) -> List[DefectReport]:
    """
    Applies the spec's Mandatory / Warning / Blocking condition rules and
    emits one DefectReport per violated condition. Never mutates code —
    QA only ever produces structured defect data for Engineering to act on.
    """
    defects: List[DefectReport] = []

    def _add(category: FailureCategory, description: str, reproduction_info: str = "") -> None:
        severity = DefectSeverity(severity_for_category(category.value))
        defects.append(DefectReport(
            defect_id=defect_id_for(project_id, category.value, module_id),
            project_id=project_id, artifact_id=None, module_id=module_id,
            commit_sha=commit_sha, pull_request_id=pull_request_id,
            severity=severity, category=category, description=description,
            reproduction_info=reproduction_info,
        ))

    if not build_succeeded:
        _add(FailureCategory.BUILD_FAILURE, "Build failed — cannot proceed to test execution.")
    if not migration_succeeded:
        _add(FailureCategory.MIGRATION_FAILURE, "Database migration failed.")
    if not contract_valid:
        _add(FailureCategory.CONTRACT_BREAK, "API contract validation failed against openapi_spec.")
    if not coverage.meets_threshold:
        _add(FailureCategory.COVERAGE_GAP,
             f"Coverage {coverage.line_coverage:.1f}% is below the {coverage.threshold_pct:.0f}% threshold.")
    if not regression.passed:
        _add(FailureCategory.REGRESSION,
             f"{regression.tests_failed} regression(s) detected: {regression.regressions_detected}",
             reproduction_info=f"tests_run={regression.tests_run}")
    if not performance.passes_threshold:
        _add(FailureCategory.PERFORMANCE,
             f"p95={performance.p95_ms}ms exceeds {performance.threshold_p95_ms}ms threshold "
             f"or error_rate={performance.error_rate_pct}% too high.")
    if not compatibility.passed:
        _add(FailureCategory.TEST_FAILURE,
             f"Compatibility issues detected: {compatibility.incompatibilities}")

    return defects


def build_qa_report(
    project_id: str,
    build_succeeded: bool,
    migration_succeeded: bool,
    contract_valid: bool,
    coverage: CoverageReport,
    regression: RegressionReport,
    performance: PerformanceReport,
    compatibility: CompatibilityReport,
    tests_total: int,
    tests_passed: int,
    tests_failed: int,
    defects: List[DefectReport],
) -> QAReport:
    """
    Aggregates all validation outcomes into the final QAReport verdict,
    per the spec's Mandatory Pass / Warning / Blocking rules:
      FAIL — any blocking defect (build/migration/contract/coverage/
             critical-regression/infra failure)
      WARN — only non-blocking issues (minor perf regressions, docs,
             lint, non-critical compatibility)
      PASS — all mandatory pass conditions satisfied, nothing to report
    """
    blocking = [d.description for d in defects if d.is_blocking]
    warnings = [d.description for d in defects if not d.is_blocking]

    if blocking:
        verdict = QAVerdict.FAIL
    elif warnings:
        verdict = QAVerdict.WARN
    else:
        verdict = QAVerdict.PASS

    return QAReport(
        project_id=project_id,
        verdict=verdict,
        blocking_conditions=blocking,
        warning_conditions=warnings,
        tests_total=tests_total,
        tests_passed=tests_passed,
        tests_failed=tests_failed,
        coverage_pct=coverage.line_coverage,
        defect_ids=[d.defect_id for d in defects],
        retry_requested=bool(blocking),
    )


def build_retry_request(project_id: str, target_team: str, reason: str, retry_count: int = 0) -> RetryRequest:
    return RetryRequest(project_id=project_id, target_team=target_team, reason=reason, retry_count=retry_count)
