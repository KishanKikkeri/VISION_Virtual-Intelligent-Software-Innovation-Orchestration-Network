"""
services/integration/dashboard/dashboard_models.py
=================================
M4.3 §0 Dashboard Models — the shared, framework-free shapes the rest
of this package passes around. Mirrors the layering convention already
established by M4.1/M4.2 (`state_diff.StateDiff`, `replay_engine.
ReplayTrace`, etc.): pure Pydantic models with no FastAPI/SQLAlchemy
imports, so `dashboard_builder.py` stays independently unit-testable
against synthetic input, same as `graph_diff`/`state_diff` before it.

Nothing here talks to a database, a workflow, or an HTTP request —
that's `dashboard_repository.py` and `dashboard_service.py`'s job.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ServiceStatus(BaseModel):
    """One row of the "Service Status" card. `name` is a department/
    service name (architecture, engineering, monitoring, ...); `status`
    is intentionally a free string ("healthy" | "degraded" | "down" |
    "unknown") rather than an enum, so a health source this dashboard
    doesn't fully understand yet still renders instead of failing
    validation."""
    name: str
    status: str = "unknown"
    detail: Optional[str] = None


class WorkflowStatusEntry(BaseModel):
    """One row of the "Workflow Status" card — M4.3 spec's four states
    (running/idle/failed/paused) plus the version/graph-hash/execution
    metadata the spec asks for, sourced from M4.1's version registry
    and M3.10's workflow_validator, not invented here."""
    name: str
    state: str = "idle"  # running | idle | failed | paused
    healthy: bool = True
    version: Optional[str] = None
    graph_hash: Optional[str] = None
    execution_count: int = 0
    errors: List[str] = Field(default_factory=list)


class EventStreamItem(BaseModel):
    """One row of the live Event Stream card. `category` and `severity`
    both support the spec's filtering/search/severity requirement.
    `severity` is derived, not stored — see dashboard_repository.py's
    `_severity_of` for the (documented, overridable) heuristic."""
    id: Optional[int] = None
    event_type: str
    category: str
    severity: str = "info"  # info | warning | error | critical
    actor_type: str = "system"
    actor_id: str = "system"
    project_id: Optional[str] = None
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    recorded_at: Optional[str] = None


class IncidentSummary(BaseModel):
    """One row of the "Active Incidents" card. Deliberately a superset
    of what a dedicated Incident Response service row and an audit-
    trail-derived fallback both naturally produce — see
    dashboard_repository.py's module docstring for why both paths
    exist."""
    id: str
    title: str
    severity: str = "info"
    status: str = "open"
    workflow: Optional[str] = None
    opened_at: Optional[str] = None
    source: str = "audit_trail"  # "incident_service" | "audit_trail"


class VersionSummary(BaseModel):
    """One row of the "Versioning" card — thin projection of M4.1's
    `VersionRegistry`/`CompatibilityChecker` output, not a new
    versioning concept."""
    workflow: str
    current_version: Optional[str] = None
    previous_version: Optional[str] = None
    is_breaking_from_previous: Optional[bool] = None
    compatible_with_previous: Optional[bool] = None


class MetricsSnapshot(BaseModel):
    """The "Metrics" card. Fixed fields are the ones the M4.3 spec
    names explicitly (workflow latency, executions, alerts,
    deployments, websocket connections); `extra` carries anything else
    the Monitoring Service reports without this model needing to
    change every time Monitoring adds a metric."""
    workflow_latency_ms: Optional[float] = None
    executions_total: Optional[int] = None
    alerts_total: Optional[int] = None
    deployments_total: Optional[int] = None
    websocket_connections: Optional[int] = None
    extra: Dict[str, Any] = Field(default_factory=dict)
    available: bool = True
    note: Optional[str] = None


class DashboardSummary(BaseModel):
    """The single-glance header row — the "Platform Health" card's
    numbers, plus enough counts from every other card that a caller
    who only fetches `/platform/dashboard/summary` still gets the
    at-a-glance picture the spec asks for."""
    generated_at: str
    overall_ready: Optional[bool] = None
    readiness_score: Optional[float] = None
    health_status: str = "unknown"
    service_count: int = 0
    healthy_service_count: int = 0
    workflow_count: int = 0
    workflows_healthy_count: int = 0
    workflows_running_count: int = 0
    workflows_failed_count: int = 0
    active_incident_count: int = 0
    recent_event_count: int = 0
    degraded_sections: List[str] = Field(default_factory=list)


class ChaosSummary(BaseModel):
    """M4.5 §12 Dashboard Integration — the chaos-testing card's data,
    sourced from `services.integration.chaos.chaos_dashboard.
    fetch_chaos_dashboard_section` (in-flight scenario state) and
    `chaos_repository.py` (latest resilience score / historical
    trend). Optional and additive: a platform with no chaos runs yet
    (or chaos DB tables not wired) simply omits this card — see
    `PlatformDashboard.chaos`."""
    running_scenarios: List[Dict[str, Any]] = Field(default_factory=list)
    active_faults: List[str] = Field(default_factory=list)
    latest_resilience_score: Optional[float] = None
    historical_trend: List[Dict[str, Any]] = Field(default_factory=list)


class SecuritySummary(BaseModel):
    """M4.6 Dashboard Integration — the security-hardening card's data,
    sourced from `services.integration.security_hardening.
    security_repository.fetch_security_dashboard_section` (latest
    posture score/status, active finding counts by severity, and
    historical trend). Optional and additive: a platform with no
    security scans yet (or security tables not wired) simply omits
    this card — see `PlatformDashboard.security`, same convention
    `ChaosSummary`/`PlatformDashboard.chaos` established for M4.5."""
    latest_posture_score: Optional[float] = None
    latest_status: Optional[str] = None
    active_finding_counts: Dict[str, int] = Field(default_factory=dict)
    historical_trend: List[Dict[str, Any]] = Field(default_factory=list)


class PluginsSummary(BaseModel):
    """M4.7 Dashboard Integration — the Plugin SDK card's data, sourced
    from `services.integration.plugin_sdk.plugin_repository.
    fetch_plugin_dashboard_section` (installed/enabled/disabled/error
    counts and a list of plugin ids currently reporting unhealthy —
    see `plugin_runtime.compute_health`). Optional and additive: a
    platform with no plugins installed (or plugin tables not wired)
    simply omits this card — see `PlatformDashboard.plugins`, same
    convention `ChaosSummary`/`SecuritySummary` established for
    M4.5/M4.6."""
    installed_count: int = 0
    enabled_count: int = 0
    disabled_count: int = 0
    error_count: int = 0
    unhealthy_plugins: List[str] = Field(default_factory=list)


class DesignerSummary(BaseModel):
    """M4.8 Dashboard Integration — the Visual Workflow Designer card's
    data, sourced from `services.integration.workflow_designer.
    designer_repository.fetch_designer_dashboard_section` (workflow
    count, recently edited workflows, and a count of workflows currently
    failing `validation_bridge.validate_layout`). Optional and additive:
    a platform with no designer workflows saved yet (or designer tables
    not wired) simply omits this card — see `PlatformDashboard.designer`,
    same convention `ChaosSummary`/`SecuritySummary`/`PluginsSummary`
    established for M4.5/M4.6/M4.7."""
    workflow_count: int = 0
    recent_edits: List[Dict[str, Any]] = Field(default_factory=list)
    invalid_count: int = 0


class ProductionSummary(BaseModel):
    """M4.9 Dashboard Integration — the Production Readiness card's
    data, sourced from `services.integration.production.
    production_repository.fetch_production_dashboard_section` (latest
    release version, latest backup timestamp/count, latest environment
    check status). Optional and additive: a platform with no releases/
    backups/checks recorded yet (or production tables not wired)
    simply omits this card — see `PlatformDashboard.production`, same
    convention `ChaosSummary`/`SecuritySummary`/`PluginsSummary`/
    `DesignerSummary` established for M4.5-M4.8."""
    latest_release_version: Optional[str] = None
    latest_backup_at: Optional[str] = None
    backup_count: int = 0
    latest_environment_status: Optional[str] = None


class ReleaseSummary(BaseModel):
    """M4.10 Dashboard Integration — the Final Release card's data
    (§3 "Release section"): readiness score, workflow count, plugin
    count, security posture, chaos score, production score, version,
    git commit, build date. Sourced by re-projecting `ChaosSummary.
    latest_resilience_score` / `SecuritySummary.latest_posture_score` /
    `PluginsSummary.installed_count` / `DesignerSummary.workflow_count`
    / `ProductionSummary` — no new score is invented here, this card
    only adds `readiness_score` (this milestone's own
    `ReleaseScore.percentage`) plus release-identity fields (version/
    git_commit/build_date) that no earlier card carries. Optional and
    additive, same convention as `ChaosSummary`/.../`ProductionSummary`
    established for M4.5-M4.9 — a platform with no readiness check run
    yet simply omits this card."""
    readiness_score: Optional[float] = None
    workflow_count: int = 0
    plugin_count: int = 0
    security_posture: Optional[float] = None
    chaos_score: Optional[float] = None
    production_score: Optional[str] = None
    version: Optional[str] = None
    git_commit: Optional[str] = None
    build_date: Optional[str] = None


class PlatformDashboard(BaseModel):
    """The full `GET /platform/dashboard` payload — every card's data
    in one response, so a first paint doesn't need seven round trips.
    Each per-card endpoint (`/dashboard/services`, `/dashboard/
    workflows`, ...) returns the corresponding slice of this same
    shape, so the SPA can either fetch this once or poll individual
    cards at different intervals."""
    generated_at: str
    summary: DashboardSummary
    services: List[ServiceStatus] = Field(default_factory=list)
    workflows: List[WorkflowStatusEntry] = Field(default_factory=list)
    events: List[EventStreamItem] = Field(default_factory=list)
    incidents: List[IncidentSummary] = Field(default_factory=list)
    versions: List[VersionSummary] = Field(default_factory=list)
    metrics: MetricsSnapshot = Field(default_factory=MetricsSnapshot)
    chaos: Optional[ChaosSummary] = None
    security: Optional[SecuritySummary] = None
    plugins: Optional[PluginsSummary] = None
    designer: Optional[DesignerSummary] = None
    production: Optional[ProductionSummary] = None
    release: Optional[ReleaseSummary] = None
    degraded_sections: List[str] = Field(default_factory=list)
