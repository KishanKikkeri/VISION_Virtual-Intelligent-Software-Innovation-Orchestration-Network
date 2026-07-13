"""
services/security/models — Stage 1 core models for M3.5 Security Service.
===========================================================================
Pure in-memory/Pydantic models used across the Security service.

Design decision (see docs/M3.5_Security_Service_Handover.md): Security
does NOT introduce new ORM tables (dependency_scans, secret_scans,
license_reports, sbom_records, security_findings, risk_reports as
described in the spec's "Database" section are represented as generic
`Artifact` rows — artifact_type = "dependency_scan", "secret_scan", etc
— exactly the pattern M3.4 QA established for its own reports. This
keeps Security consistent with the platform-wide artifact storage
convention instead of hand-rolling Security-specific tables.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Enums ─────────────────────────────────────────────────────

class ScanTeam(str, Enum):
    DEPENDENCY = "dependency"
    CODE       = "code"
    COMPLIANCE = "compliance"


class FindingSeverity(str, Enum):
    CRITICAL = "critical"
    HIGH     = "high"
    MEDIUM   = "medium"
    LOW      = "low"


class FindingCategory(str, Enum):
    CVE                    = "cve"
    SECRET                 = "secret"
    OWASP                  = "owasp"
    INJECTION              = "injection"
    LICENSE                = "license"
    COMPLIANCE_VIOLATION   = "compliance_violation"
    HIGH_RISK_DEPENDENCY   = "high_risk_dependency"


class SecurityVerdict(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class SecurityTaskStatus(str, Enum):
    PENDING       = "pending"
    RUNNING       = "running"
    COMPLETED     = "completed"
    FAILED        = "failed"
    ESCALATED     = "escalated"
    DEAD_LETTERED = "dead_lettered"


# ── Dependency / manifest ─────────────────────────────────────

class DependencyEntry(BaseModel):
    name:    str
    version: str = "unknown"
    ecosystem: str = "unknown"     # pypi | npm | unknown


class DependencyManifest(BaseModel):
    project_id:   str
    dependencies: List[DependencyEntry] = Field(default_factory=list)
    source_files: List[str] = Field(default_factory=list)


# ── Scan artifacts ────────────────────────────────────────────

class Vulnerability(BaseModel):
    package:     str
    version:     str = "unknown"
    cve_id:      str
    severity:    FindingSeverity
    description: str = ""


class DependencyScan(BaseModel):
    project_id:          str
    dependencies_scanned: int = 0
    vulnerabilities:      List[Vulnerability] = Field(default_factory=list)

    @property
    def critical_count(self) -> int:
        return sum(1 for v in self.vulnerabilities if v.severity == FindingSeverity.CRITICAL)

    @property
    def high_count(self) -> int:
        return sum(1 for v in self.vulnerabilities if v.severity == FindingSeverity.HIGH)

    @property
    def has_critical(self) -> bool:
        return self.critical_count > 0


class CodeIssue(BaseModel):
    rule:        str
    file:        str = "unknown"
    severity:    FindingSeverity
    description: str = ""


class StaticAnalysisReport(BaseModel):
    """
    Combines the spec's OWASP-pattern check and injection check into a
    single Static Analysis Report artifact (see docs/M3.5 handover,
    "Department Structure" deviation note).
    """
    project_id:       str
    files_scanned:    int = 0
    owasp_findings:   List[CodeIssue] = Field(default_factory=list)
    injection_findings: List[CodeIssue] = Field(default_factory=list)

    @property
    def all_findings(self) -> List[CodeIssue]:
        return self.owasp_findings + self.injection_findings

    @property
    def has_critical(self) -> bool:
        return any(f.severity == FindingSeverity.CRITICAL for f in self.all_findings)


class SecretHit(BaseModel):
    file:        str
    rule:        str          # e.g. "aws_access_key", "private_key", "generic_api_key"
    line:        int = 0
    severity:    FindingSeverity = FindingSeverity.CRITICAL


class SecretScan(BaseModel):
    project_id:   str
    files_scanned: int = 0
    secrets:      List[SecretHit] = Field(default_factory=list)

    @property
    def secret_count(self) -> int:
        return len(self.secrets)

    @property
    def has_secrets(self) -> bool:
        return self.secret_count > 0


class SBOMComponent(BaseModel):
    name:      str
    version:   str = "unknown"
    ecosystem: str = "unknown"
    license:   str = "unknown"


class SBOM(BaseModel):
    project_id: str
    components: List[SBOMComponent] = Field(default_factory=list)
    format:     str = "CycloneDX-lite"

    @property
    def component_count(self) -> int:
        return len(self.components)


class LicenseReport(BaseModel):
    project_id:           str
    licenses_found:       Dict[str, int] = Field(default_factory=dict)
    disallowed_licenses:  List[str] = Field(default_factory=list)

    @property
    def compliant(self) -> bool:
        return not self.disallowed_licenses


class ComplianceReport(BaseModel):
    project_id: str
    checklist:  Dict[str, bool] = Field(default_factory=dict)
    violations: List[str] = Field(default_factory=list)

    @property
    def compliant(self) -> bool:
        return not self.violations


class RiskAssessment(BaseModel):
    project_id:           str
    risk_score:           float = 0.0   # 0-100
    risk_level:           str = "low"   # low | medium | high | critical
    contributing_factors: List[str] = Field(default_factory=list)


# ── Findings & reporting ──────────────────────────────────────

class SecurityFinding(BaseModel):
    """
    Security never edits code — every finding becomes a structured
    SecurityFinding routed back to Engineering via Manager Service,
    mirroring QA's DefectReport.
    """
    finding_id:      str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id:      str
    category:        FindingCategory
    severity:        FindingSeverity
    description:     str
    source_worker:   str
    module_id:        Optional[str] = None
    created_at:       datetime = Field(default_factory=datetime.utcnow)

    @property
    def is_blocking(self) -> bool:
        return self.severity in (FindingSeverity.CRITICAL, FindingSeverity.HIGH)


class RetryRequest(BaseModel):
    retry_id:    str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id:  str
    target_team: str
    reason:      str
    retry_count: int = 0
    max_retries: int = 3
    created_at:  datetime = Field(default_factory=datetime.utcnow)

    @property
    def can_retry(self) -> bool:
        return self.retry_count < self.max_retries


class SecurityReport(BaseModel):
    """Final Stage-result of the Security pipeline — the Risk Lead's summary output."""
    report_id:           str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id:          str
    verdict:             SecurityVerdict = SecurityVerdict.FAIL
    blocking_conditions: List[str] = Field(default_factory=list)
    warning_conditions:  List[str] = Field(default_factory=list)
    finding_ids:         List[str] = Field(default_factory=list)
    risk_level:          str = "low"
    risk_score:          float = 0.0
    retry_requested:     bool = False
    created_at:          datetime = Field(default_factory=datetime.utcnow)


# ── Security task / plan (mirrors QATask / QAPlan) ────────────

class SecurityTask(BaseModel):
    task_id:         str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id:      str
    team:            ScanTeam
    worker_agent_id: str
    description:     str = ""
    depends_on:      List[str] = Field(default_factory=list)
    status:          SecurityTaskStatus = SecurityTaskStatus.PENDING
    retry_count:     int = 0
    max_retries:     int = 3
    failure_reason:  Optional[str] = None
    escalated:       bool = False
    dead_lettered:   bool = False

    def can_run(self, completed_task_ids: set) -> bool:
        return (
            self.status == SecurityTaskStatus.PENDING
            and all(dep in completed_task_ids for dep in self.depends_on)
        )

    def next_backoff_seconds(self) -> int:
        return min(60, 2 ** max(0, self.retry_count))


class SecurityPlan(BaseModel):
    plan_id:          str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id:       str
    feature_name:     str
    tasks:            List[SecurityTask] = Field(default_factory=list)
    engineering_refs: Dict[str, Any] = Field(default_factory=dict)
    created_at:       datetime = Field(default_factory=datetime.utcnow)

    def ready_tasks(self, completed_task_ids: set) -> List[SecurityTask]:
        return [t for t in self.tasks if t.can_run(completed_task_ids)]

    def tasks_by_team(self, team: ScanTeam) -> List[SecurityTask]:
        return [t for t in self.tasks if t.team == team]

    @property
    def all_complete(self) -> bool:
        return all(t.status == SecurityTaskStatus.COMPLETED for t in self.tasks)

    @property
    def any_dead_lettered(self) -> bool:
        return any(t.dead_lettered for t in self.tasks)
