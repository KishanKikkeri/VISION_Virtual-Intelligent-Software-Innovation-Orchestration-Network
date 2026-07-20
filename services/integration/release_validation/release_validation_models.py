"""
services/integration/release_validation/release_validation_models.py
=================================
M4.10 §1 Release Validation — pure Pydantic shapes, no FastAPI/SQLAlchemy
import anywhere in this module. Same layering convention M4.9's
`release_models.py` established one level down: plain data shapes so
`release_validator.py`/`compatibility_matrix.py`/`dependency_checker.py`/
`environment_audit.py`/`benchmark_summary.py`/`readiness_report.py` stay
independently unit-testable.

**Scope note.** M4.10 is explicitly "not another feature milestone" (see
docs/M4.10_Final_Release_Handover.md's mission statement) — this package
never redesigns M4.9's `production/` package, it *consumes* it. Where a
check needs a real M3.x/M4.x module this sandbox slice doesn't include
(Monitoring, Chaos Engineering, Security Hardening — see M4.9 handover §3
for the standing scope note this milestone inherits), the corresponding
function degrades gracefully (`available=False` / `CheckStatus.SKIPPED`),
exactly like M4.9's `environment_validator.py` does.
"""
from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class CheckStatus(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIPPED = "skipped"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class ExportFormat(str, Enum):
    JSON = "json"
    MARKDOWN = "markdown"
    HTML = "html"


# ── §1a Release Validator ────────────────────────────────────────────

class ReleaseCheckItem(BaseModel):
    name: str
    category: str  # "dependency" | "compatibility" | "environment" | "benchmark" | "documentation" | "qa"
    status: CheckStatus
    detail: str = ""
    weight: float = 1.0


class ReleaseScore(BaseModel):
    """0-100 weighted rollup of all ReleaseCheckItem results contributing
    to release readiness. PASS contributes full weight, WARN contributes
    half weight, FAIL/SKIPPED contribute zero — the same "FAIL dominates"
    spirit as M4.9's `EnvironmentReport.overall_status`, expressed as a
    number instead of an enum because §9 Final QA and the CLI both need a
    single sortable/thresholdable figure, not just a status."""
    score: float
    max_score: float
    checks: List[ReleaseCheckItem] = Field(default_factory=list)

    @property
    def percentage(self) -> float:
        return round((self.score / self.max_score) * 100, 2) if self.max_score else 0.0

    @property
    def grade(self) -> str:
        pct = self.percentage
        if pct >= 95:
            return "release_candidate"
        if pct >= 80:
            return "near_ready"
        if pct >= 50:
            return "needs_work"
        return "not_ready"


# ── §1b Compatibility Matrix ──────────────────────────────────────────

class CompatibilityEntry(BaseModel):
    component: str  # e.g. "python", "postgres", "redis", "nats", "docker"
    required_range: str  # e.g. ">=3.11,<4.0"
    detected_version: Optional[str] = None
    compatible: Optional[bool] = None  # None => undetermined (component not present)
    note: str = ""


class CompatibilityMatrix(BaseModel):
    entries: List[CompatibilityEntry] = Field(default_factory=list)

    @property
    def all_compatible(self) -> bool:
        return all(e.compatible is not False for e in self.entries)

    @property
    def undetermined_count(self) -> int:
        return sum(1 for e in self.entries if e.compatible is None)


# ── §1c Dependency Checker ────────────────────────────────────────────

class DependencyIssue(BaseModel):
    name: str
    kind: str  # "missing" | "version_conflict" | "duplicate" | "unpinned"
    detail: str
    severity: Severity = Severity.WARNING


class DependencyReport(BaseModel):
    total_dependencies: int
    issues: List[DependencyIssue] = Field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not any(i.severity in (Severity.ERROR, Severity.CRITICAL) for i in self.issues)


# ── §1d Environment Audit ─────────────────────────────────────────────

class EnvironmentAuditItem(BaseModel):
    name: str
    category: str
    status: CheckStatus
    detail: str = ""


class EnvironmentAuditReport(BaseModel):
    items: List[EnvironmentAuditItem] = Field(default_factory=list)

    @property
    def overall_status(self) -> CheckStatus:
        statuses = {i.status for i in self.items}
        if CheckStatus.FAIL in statuses:
            return CheckStatus.FAIL
        if CheckStatus.WARN in statuses:
            return CheckStatus.WARN
        if statuses == {CheckStatus.SKIPPED}:
            return CheckStatus.SKIPPED
        return CheckStatus.PASS


# ── §1e / §7 Benchmark Summary ────────────────────────────────────────

class BenchmarkMetric(BaseModel):
    name: str  # e.g. "workflow_speed", "agent_latency"
    unit: str  # "ms", "ops_sec", ...
    value: float
    baseline: Optional[float] = None

    @property
    def regression(self) -> Optional[bool]:
        """True if value is worse than baseline by more than 10%. Higher
        is assumed better for *_sec/_throughput-style units, lower is
        assumed better for latency/duration-style units — see
        `benchmark_summary.is_higher_better` for the actual heuristic;
        this property only asks "did the run supply a baseline"."""
        if self.baseline is None:
            return None
        return None  # computed by benchmark_summary.detect_regressions, not here


class BenchmarkSummaryReport(BaseModel):
    metrics: List[BenchmarkMetric] = Field(default_factory=list)
    generated_at: str = ""


# ── §1f Readiness Report (the unified Release Report) ─────────────────

class DocumentationCompleteness(BaseModel):
    expected_documents: List[str] = Field(default_factory=list)
    present_documents: List[str] = Field(default_factory=list)

    @property
    def missing_documents(self) -> List[str]:
        return [d for d in self.expected_documents if d not in self.present_documents]

    @property
    def completeness_pct(self) -> float:
        if not self.expected_documents:
            return 100.0
        return round(100.0 * len(self.present_documents) / len(self.expected_documents), 2)


class ReadinessReport(BaseModel):
    """§1 'Produce one unified Release Report.' Every sub-report this
    package can build, in one shape — deliberately a thin container over
    the other models above, not a re-derivation of their data, so there
    is exactly one source of truth for each figure it shows."""
    version: str
    release_score: ReleaseScore
    dependency_report: DependencyReport
    compatibility_matrix: CompatibilityMatrix
    environment_audit: EnvironmentAuditReport
    benchmark_summary: BenchmarkSummaryReport
    documentation_completeness: DocumentationCompleteness
    generated_at: str = ""

    @property
    def release_candidate_ready(self) -> bool:
        return (
            self.release_score.grade in ("release_candidate", "near_ready")
            and self.dependency_report.clean
            and self.compatibility_matrix.all_compatible
            and self.environment_audit.overall_status in (CheckStatus.PASS, CheckStatus.SKIPPED)
        )


# ── §9 Final QA ────────────────────────────────────────────────────────

class QACheckResult(BaseModel):
    name: str  # "workflow_validation" | "lint" | "replay" | "chaos" | "security" | "production" | "plugins"
    status: CheckStatus
    detail: str = ""
    duration_ms: Optional[float] = None


class QAReport(BaseModel):
    checks: List[QACheckResult] = Field(default_factory=list)
    generated_at: str = ""

    @property
    def passed(self) -> bool:
        return all(c.status in (CheckStatus.PASS, CheckStatus.SKIPPED) for c in self.checks)

    @property
    def pass_count(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.PASS)

    @property
    def fail_count(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.FAIL)

    @property
    def skipped_count(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.SKIPPED)


# ── §8 Release Packaging ──────────────────────────────────────────────

class ReleaseManifestEntry(BaseModel):
    path: str
    kind: str  # "changelog" | "release_notes" | "license" | "notice" | "version" | "docs" | "benchmark"
    present: bool
    checksum: Optional[str] = None


class ReleaseManifest(BaseModel):
    version: str
    entries: List[ReleaseManifestEntry] = Field(default_factory=list)
    generated_at: str = ""

    @property
    def complete(self) -> bool:
        return all(e.present for e in self.entries)


# ── §4 Installation Wizard ─────────────────────────────────────────────

class InstallCheckResult(BaseModel):
    name: str  # "python" | "dependencies" | "docker" | "redis" | "postgres" | "nats"
    status: CheckStatus
    detail: str = ""
    fix_suggestion: Optional[str] = None


class InstallReport(BaseModel):
    checks: List[InstallCheckResult] = Field(default_factory=list)

    @property
    def ready(self) -> bool:
        return all(c.status in (CheckStatus.PASS, CheckStatus.SKIPPED) for c in self.checks)
