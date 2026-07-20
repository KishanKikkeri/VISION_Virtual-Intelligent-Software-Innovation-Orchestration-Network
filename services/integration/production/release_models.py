"""
services/integration/production/release_models.py
=================================
M4.9 — pure Pydantic shapes, no FastAPI/SQLAlchemy/Docker/Kubernetes client
import anywhere in this module. Same layering convention every prior M4.x
milestone in this slice established (`designer_models.py`, `plugin_models.py`):
plain data shapes so `configuration_manager.py`/`environment_validator.py`/
`deployment_validator.py`/`release_manager.py`/`backup_manager.py`/
`restore_manager.py` stay independently unit-testable, with no persistence
or HTTP machinery anywhere near the data shapes themselves.

**Scope note.** This package is deliberately the *last* layer added to the
platform (§Mission: "hardening... not... major capabilities"). Nothing here
introduces a new runtime concept — `DeploymentProfile` describes how the
*existing* eleven services/ten workflows are configured per environment; it
does not add an eleventh service. Where a check or export needs a real
integration this sandbox slice doesn't include (the M4.6 SBOM generator, a
live NATS/Redis client, a real Kubernetes API), the corresponding module
degrades gracefully rather than failing — see each module's own docstring.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class Environment(str, Enum):
    DEVELOPMENT = "development"
    TESTING = "testing"
    STAGING = "staging"
    PRODUCTION = "production"


class ConfigFormat(str, Enum):
    YAML = "yaml"
    JSON = "json"
    ENV = "env"


class CheckStatus(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIPPED = "skipped"  # optional infrastructure not available — degrade, don't fail the run


class BackupType(str, Enum):
    FULL = "full"
    INCREMENTAL = "incremental"


class RestoreMode(str, Enum):
    DRY_RUN = "dry_run"
    FULL = "full"
    SELECTIVE = "selective"


class BackupScope(str, Enum):
    """§6 Backup Manager's six named scopes, verbatim."""
    DATABASE = "database"
    ARTIFACTS = "artifacts"
    WORKFLOW_LAYOUTS = "workflow_layouts"
    PLUGIN_REGISTRY = "plugin_registry"
    CONFIGURATION = "configuration"
    AUDIT_LOGS = "audit_logs"


class DeploymentAssetKind(str, Enum):
    DOCKER_COMPOSE = "docker_compose"
    KUBERNETES_MANIFEST = "kubernetes_manifest"
    HELM_VALUES = "helm_values"
    ENVIRONMENT_CONFIG = "environment_config"


class ExportFormat(str, Enum):
    JSON = "json"
    MARKDOWN = "markdown"
    HTML = "html"


# ── §2 Configuration Manager ─────────────────────────────────────────

class ConfigValidationIssue(BaseModel):
    kind: str  # "missing" | "conflicting" | "deprecated" | "unknown"
    key: str
    message: str
    severity: str = "error"  # "error" | "warning"


class ConfigValidationResult(BaseModel):
    environment: str
    valid: bool = True
    issues: List[ConfigValidationIssue] = Field(default_factory=list)

    @property
    def errors(self) -> List[ConfigValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> List[ConfigValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]


class ConfigDiffEntry(BaseModel):
    key: str
    kind: str  # "added" | "removed" | "changed"
    from_value: Optional[Any] = None
    to_value: Optional[Any] = None


class ConfigDiffResult(BaseModel):
    entries: List[ConfigDiffEntry] = Field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return len(self.entries) > 0


class ConfigCompareResult(BaseModel):
    """§2 `compare()` — a shallower, human-facing summary than `diff()`'s
    full entry list: just which top-level sections differ, for a quick
    "did anything change" glance (`diff()` is the detailed version)."""
    matching_sections: List[str] = Field(default_factory=list)
    differing_sections: List[str] = Field(default_factory=list)


# ── §1 Environment Profiles ──────────────────────────────────────────

class DeploymentProfile(BaseModel):
    """One environment's fully-merged (base -> environment -> override)
    configuration document. `sections` covers the brief's nine named
    areas (database/messaging/cache/logging/security/monitoring/plugins/
    dashboard/workflow settings) as a free-form dict-of-dicts so this
    model never needs to change when a section grows a new key."""
    environment: Environment
    sections: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    overrides_applied: List[str] = Field(default_factory=list)
    generated_at: str = ""


# ── §3 Environment Validator ──────────────────────────────────────────

class EnvironmentCheckItem(BaseModel):
    name: str
    category: str
    status: CheckStatus = CheckStatus.SKIPPED
    detail: str = ""
    remediation: Optional[str] = None


class EnvironmentReport(BaseModel):
    environment: str
    checks: List[EnvironmentCheckItem] = Field(default_factory=list)
    generated_at: str = ""

    @property
    def overall_status(self) -> CheckStatus:
        statuses = {c.status for c in self.checks}
        if CheckStatus.FAIL in statuses:
            return CheckStatus.FAIL
        if CheckStatus.WARN in statuses:
            return CheckStatus.WARN
        if not statuses or statuses == {CheckStatus.SKIPPED}:
            return CheckStatus.SKIPPED
        return CheckStatus.PASS

    @property
    def ready(self) -> bool:
        return self.overall_status in (CheckStatus.PASS, CheckStatus.SKIPPED)


# ── §4 Deployment Validator ───────────────────────────────────────────

class DeploymentValidationIssue(BaseModel):
    asset: str
    kind: DeploymentAssetKind
    message: str
    severity: str = "error"


class DeploymentValidationResult(BaseModel):
    valid: bool = True
    assets_checked: List[str] = Field(default_factory=list)
    issues: List[DeploymentValidationIssue] = Field(default_factory=list)


# ── §5 Release Manager ────────────────────────────────────────────────

class SemanticVersion(BaseModel):
    major: int = 0
    minor: int = 0
    patch: int = 0
    prerelease: Optional[str] = None

    def __str__(self) -> str:  # pragma: no cover — trivial
        base = f"{self.major}.{self.minor}.{self.patch}"
        return f"{base}-{self.prerelease}" if self.prerelease else base


class ArtifactEntry(BaseModel):
    name: str
    kind: str = "module"  # "module" | "migration" | "static_asset" | "doc"
    path: str = ""


class DependencyEntry(BaseModel):
    name: str
    version: str = ""
    source: str = "requirements.txt"


class SBOMReference(BaseModel):
    """Reuses M4.6's SBOM generator when available (see
    `release_manager.py`'s module docstring); `available=False` +
    `note` when that module isn't wired in this sandbox slice."""
    available: bool = False
    format: Optional[str] = None
    component_count: int = 0
    location: Optional[str] = None
    note: Optional[str] = None


class ChecklistStep(BaseModel):
    step: str
    detail: str = ""
    required: bool = True


class Release(BaseModel):
    version: str
    previous_version: Optional[str] = None
    channel: str = "stable"  # "stable" | "staging" | "canary"
    release_notes: List[str] = Field(default_factory=list)
    artifacts: List[ArtifactEntry] = Field(default_factory=list)
    dependencies: List[DependencyEntry] = Field(default_factory=list)
    sbom: SBOMReference = Field(default_factory=SBOMReference)
    upgrade_checklist: List[ChecklistStep] = Field(default_factory=list)
    rollback_plan: List[ChecklistStep] = Field(default_factory=list)
    created_at: str = ""


# ── §6/§7 Backup / Restore ───────────────────────────────────────────

class BackupRecord(BaseModel):
    id: str = ""
    backup_type: BackupType = BackupType.FULL
    scopes: List[BackupScope] = Field(default_factory=list)
    location: str = ""
    checksum: str = ""
    size_bytes: int = 0
    status: str = "completed"  # "completed" | "failed" | "partial"
    baseline_backup_id: Optional[str] = None  # set for incremental backups
    notes: List[str] = Field(default_factory=list)
    created_at: str = ""


class RestoreRecord(BaseModel):
    id: str = ""
    backup_id: str = ""
    mode: RestoreMode = RestoreMode.DRY_RUN
    scopes: List[BackupScope] = Field(default_factory=list)
    status: str = "planned"  # "planned" | "validated" | "applied" | "rejected" | "failed"
    confirmed: bool = False
    validation_issues: List[str] = Field(default_factory=list)
    created_at: str = ""


# ── §9 Production Checklist / Status ─────────────────────────────────

class ProductionChecklistItem(BaseModel):
    key: str
    label: str
    satisfied: bool = False
    detail: str = ""


class ProductionChecklist(BaseModel):
    environment: str
    items: List[ProductionChecklistItem] = Field(default_factory=list)
    generated_at: str = ""

    @property
    def ready(self) -> bool:
        return all(i.satisfied for i in self.items) if self.items else False

    @property
    def completion_ratio(self) -> float:
        if not self.items:
            return 0.0
        return sum(1 for i in self.items if i.satisfied) / len(self.items)


class ConfigurationDrift(BaseModel):
    """Dashboard "Configuration Drift" widget's data — the diff between
    a `DeploymentProfile`'s declared sections and whatever config was
    last actually loaded/validated for that environment."""
    environment: str
    drifted_keys: List[str] = Field(default_factory=list)
    checked_at: str = ""

    @property
    def has_drift(self) -> bool:
        return len(self.drifted_keys) > 0


class ProductionStatus(BaseModel):
    """The single-glance §API `GET /production/status` payload —
    enough of every sub-report's headline for a dashboard card without
    a caller needing to fetch all six report endpoints separately."""
    environment: str
    generated_at: str = ""
    environment_ready: Optional[bool] = None
    latest_release_version: Optional[str] = None
    latest_backup_id: Optional[str] = None
    latest_backup_at: Optional[str] = None
    checklist_ready: Optional[bool] = None
    checklist_completion: float = 0.0
    deployment_valid: Optional[bool] = None
    config_drift: bool = False
