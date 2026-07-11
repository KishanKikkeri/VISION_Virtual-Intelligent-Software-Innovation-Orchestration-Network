"""
services/qa/models — Stage 1 core models for M3.4 QA Service.
==================================================================
Pure in-memory/Pydantic models used across the QA service.

Design decision (see docs/M3.4_QA_Service_Handover.md):
QA does NOT introduce new ORM tables. `Artifact` (generic, already in
infrastructure/database/models.py) stores every artifact type QA
produces — unit_test_suite, integration_test_suite, regression_report,
performance_report, coverage_report, defect_report, qa_report — the
same way Engineering stores source_code and Architecture stores
blueprints. This keeps QA consistent with the platform-wide artifact
storage pattern instead of hand-rolling QA-specific tables.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Enums ─────────────────────────────────────────────────────

class QATeam(str, Enum):
    UNIT        = "unit"
    INTEGRATION = "integration"
    REGRESSION  = "regression"
    PERFORMANCE = "performance"


class SuiteType(str, Enum):
    UNIT        = "unit"
    INTEGRATION = "integration"
    REGRESSION  = "regression"
    PERFORMANCE = "performance"


class QAVerdict(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class DefectSeverity(str, Enum):
    CRITICAL = "critical"
    HIGH     = "high"
    MEDIUM   = "medium"
    LOW      = "low"


class FailureCategory(str, Enum):
    BUILD_FAILURE      = "build_failure"
    MIGRATION_FAILURE  = "migration_failure"
    CONTRACT_BREAK     = "contract_break"
    COVERAGE_GAP       = "coverage_gap"
    REGRESSION         = "regression"
    PERFORMANCE        = "performance"
    INFRA_FAILURE      = "test_infrastructure_failure"
    TEST_FAILURE       = "test_failure"


class QATaskStatus(str, Enum):
    PENDING       = "pending"
    RUNNING       = "running"
    COMPLETED     = "completed"
    FAILED        = "failed"
    ESCALATED     = "escalated"
    DEAD_LETTERED = "dead_lettered"


# ── Test artifacts ────────────────────────────────────────────

class TestFile(BaseModel):
    __test__ = False  # not a pytest test class — silences collection warning
    path:     str
    language: str
    content:  str


class TestSuite(BaseModel):
    __test__ = False  # not a pytest test class — silences collection warning
    """
    The unit of output produced by every QA test-generation worker.
    Mirrors Engineering's CodeModule so the same idempotency/quality
    conventions apply to generated test code.
    """
    suite_id:       str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id:     str
    task_id:        str
    suite_type:     SuiteType
    files:          List[TestFile] = Field(default_factory=list)
    test_count:     int = 0
    quality_score:  float = 0.0
    generated_by:   str
    idempotent_key: Optional[str] = None
    created_at:     datetime = Field(default_factory=datetime.utcnow)

    @property
    def is_executable(self) -> bool:
        return bool(self.files) and self.test_count > 0

    def satisfies_test_contract(self) -> List[str]:
        """Mirrors Engineering's coding-contract check, applied to test suites."""
        violations = []
        if not self.files:
            violations.append("executable")
        if self.test_count <= 0:
            violations.append("non_empty")
        if self.quality_score < 0.7:
            violations.append("reviewable")
        if not self.idempotent_key:
            violations.append("idempotent")
        return violations


# ── QA task (mirrors EngineeringTask) ─────────────────────────

class QATask(BaseModel):
    """One unit of dependency-scheduled work assigned to a QA worker agent."""
    task_id:         str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id:      str
    team:            QATeam
    worker_agent_id: str
    description:     str = ""
    depends_on:      List[str] = Field(default_factory=list)
    status:          QATaskStatus = QATaskStatus.PENDING
    retry_count:     int = 0
    max_retries:     int = 3
    result_suite_id: Optional[str] = None
    failure_reason:  Optional[str] = None
    escalated:       bool = False
    dead_lettered:   bool = False

    def can_run(self, completed_task_ids: set) -> bool:
        return (
            self.status == QATaskStatus.PENDING
            and all(dep in completed_task_ids for dep in self.depends_on)
        )

    def next_backoff_seconds(self) -> int:
        return min(60, 2 ** max(0, self.retry_count))


class QAPlan(BaseModel):
    """Stage-1 output: the full task graph derived from Engineering artifacts."""
    plan_id:            str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id:         str
    feature_name:       str
    tasks:              List[QATask] = Field(default_factory=list)
    engineering_refs:   Dict[str, Any] = Field(default_factory=dict)
    created_at:         datetime = Field(default_factory=datetime.utcnow)

    def ready_tasks(self, completed_task_ids: set) -> List[QATask]:
        return [t for t in self.tasks if t.can_run(completed_task_ids)]

    def tasks_by_team(self, team: QATeam) -> List[QATask]:
        return [t for t in self.tasks if t.team == team]

    @property
    def all_complete(self) -> bool:
        return all(t.status == QATaskStatus.COMPLETED for t in self.tasks)

    @property
    def any_dead_lettered(self) -> bool:
        return any(t.dead_lettered for t in self.tasks)


# ── Reports ───────────────────────────────────────────────────

class CoverageReport(BaseModel):
    project_id:         str
    line_coverage:       float = 0.0
    branch_coverage:     float = 0.0
    function_coverage:   float = 0.0
    threshold_pct:       float = 80.0
    meets_threshold:     bool = False

    @classmethod
    def build(cls, project_id: str, line_coverage: float, threshold_pct: float = 80.0) -> "CoverageReport":
        line_coverage = max(0.0, min(100.0, line_coverage))
        return cls(
            project_id=project_id,
            line_coverage=line_coverage,
            branch_coverage=max(0.0, line_coverage - 5.0),
            function_coverage=min(100.0, line_coverage + 2.0),
            threshold_pct=threshold_pct,
            meets_threshold=line_coverage >= threshold_pct,
        )


class RegressionReport(BaseModel):
    project_id:            str
    tests_run:              int = 0
    tests_passed:           int = 0
    tests_failed:           int = 0
    regressions_detected:   List[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.tests_failed == 0 and not self.regressions_detected


class PerformanceReport(BaseModel):
    project_id:            str
    p95_ms:                 float = 0.0
    p99_ms:                 float = 0.0
    avg_ms:                 float = 0.0
    concurrent_users:       int = 100
    requests_per_second:    float = 0.0
    error_rate_pct:         float = 0.0
    threshold_p95_ms:       float = 500.0

    @property
    def passes_threshold(self) -> bool:
        return self.p95_ms < self.threshold_p95_ms and self.error_rate_pct < 5.0


class CompatibilityReport(BaseModel):
    project_id:      str
    environments:     List[str] = Field(default_factory=lambda: ["linux/py3.11", "linux/py3.12"])
    incompatibilities: List[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.incompatibilities


# ── Defects & retries ─────────────────────────────────────────

class DefectReport(BaseModel):
    """
    QA never edits code — every failure becomes a structured DefectReport
    routed back to Engineering via Manager Service.
    """
    defect_id:            str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id:            str
    artifact_id:           Optional[str] = None
    module_id:             Optional[str] = None
    commit_sha:            Optional[str] = None
    pull_request_id:       Optional[str] = None
    severity:              DefectSeverity
    category:              FailureCategory
    description:           str
    reproduction_info:     str = ""
    created_by:            str = "qa_head"
    created_at:            datetime = Field(default_factory=datetime.utcnow)

    @property
    def is_blocking(self) -> bool:
        return self.severity in (DefectSeverity.CRITICAL, DefectSeverity.HIGH)


class RetryRequest(BaseModel):
    retry_id:      str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id:    str
    target_team:   str
    reason:        str
    retry_count:   int = 0
    max_retries:   int = 3
    created_at:    datetime = Field(default_factory=datetime.utcnow)

    @property
    def can_retry(self) -> bool:
        return self.retry_count < self.max_retries


class QAReport(BaseModel):
    """Final Stage-6 result of the QA pipeline — Reporting Lead's summary output."""
    report_id:           str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id:          str
    verdict:             QAVerdict = QAVerdict.FAIL
    blocking_conditions: List[str] = Field(default_factory=list)
    warning_conditions:  List[str] = Field(default_factory=list)
    tests_total:         int = 0
    tests_passed:        int = 0
    tests_failed:        int = 0
    coverage_pct:        float = 0.0
    defect_ids:          List[str] = Field(default_factory=list)
    retry_requested:     bool = False
    created_at:          datetime = Field(default_factory=datetime.utcnow)
