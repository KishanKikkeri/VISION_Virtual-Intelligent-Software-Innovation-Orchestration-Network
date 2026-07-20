"""
services/integration/dashboard/dashboard_builder.py
=================================
M4.3 §1 Dashboard Builder — the pure "algorithm" layer (architectural
decision #3: pure algorithm -> repository wrapper -> API). Every
function here takes already-fetched plain data (dicts/lists — the
normalized shape any of health_validator / workflow_validator /
event_router / version_registry / a monitoring metrics call / the
audit trail can be reduced to) and returns one of `dashboard_models`'
Pydantic models. Nothing here imports FastAPI, SQLAlchemy, or any
department module directly, so it's unit-testable against synthetic
input exactly like `state_diff.diff_states` or `graph_diff` are.

`dashboard_service.py` is the layer that actually calls the real
platform modules, normalizes their output, and hands it to the
functions here. That separation is deliberate: if a department's
report shape changes, only the small "normalize" step in
`dashboard_service.py` needs to change — the assembly logic and its
tests, below, don't.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from services.integration.dashboard.dashboard_models import (
    ChaosSummary, DashboardSummary, DesignerSummary, EventStreamItem, IncidentSummary, MetricsSnapshot,
    PlatformDashboard, PluginsSummary, ProductionSummary, ReleaseSummary, SecuritySummary, ServiceStatus,
    VersionSummary, WorkflowStatusEntry,
)

_ERROR_HINTS = ("fail", "error", "reject", "crash", "denied")
_WARNING_HINTS = ("warn", "degrad", "retry", "timeout", "conflict")
_CRITICAL_HINTS = ("critical", "outage", "incident", "down")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Event stream ──────────────────────────────────────────────────

def categorize_event_type(event_type: str) -> str:
    """Same convention `execution_timeline._category_of` already
    established for this codebase (first dot-segment of `event_type`)
    — reused, not reinvented, per architectural decision #4."""
    return event_type.split(".", 1)[0] if event_type else "unknown"


def infer_severity(event_type: str, category: str) -> str:
    """Heuristic, not a stored field: `AuditEvent` has no severity
    column today, so this derives one from the event_type/category
    text so the Event Stream card's severity filter has something to
    filter on. Documented as a heuristic rather than silently presented
    as authoritative — a dedicated severity field on a future event
    schema should take priority over this the moment one exists."""
    haystack = f"{event_type} {category}".lower()
    if any(h in haystack for h in _CRITICAL_HINTS):
        return "critical"
    if any(h in haystack for h in _ERROR_HINTS):
        return "error"
    if any(h in haystack for h in _WARNING_HINTS):
        return "warning"
    return "info"


def build_event_stream_item(raw: Dict[str, Any]) -> EventStreamItem:
    event_type = raw.get("event_type", "")
    category = raw.get("category") or categorize_event_type(event_type)
    severity = raw.get("severity") or infer_severity(event_type, category)
    return EventStreamItem(
        id=raw.get("id"),
        event_type=event_type,
        category=category,
        severity=severity,
        actor_type=raw.get("actor_type", "system"),
        actor_id=raw.get("actor_id", "system"),
        project_id=raw.get("project_id"),
        entity_type=raw.get("entity_type"),
        entity_id=raw.get("entity_id"),
        recorded_at=raw.get("recorded_at"),
    )


def build_event_stream(
    raw_events: List[Dict[str, Any]],
    category: Optional[str] = None,
    severity: Optional[str] = None,
    search: Optional[str] = None,
) -> List[EventStreamItem]:
    """Filtering/search/severity, as the M4.3 spec's Event Stream card
    requires. Filters are applied here (post-normalization) rather than
    pushed into the DB query, so the same filtering logic works
    identically whether events came from the audit trail, a future
    dedicated event-stream table, or a unit test's synthetic list."""
    items = [build_event_stream_item(e) for e in raw_events]
    if category:
        items = [i for i in items if i.category == category]
    if severity:
        items = [i for i in items if i.severity == severity]
    if search:
        needle = search.lower()
        items = [
            i for i in items
            if needle in i.event_type.lower()
            or (i.entity_id and needle in str(i.entity_id).lower())
            or (i.project_id and needle in str(i.project_id).lower())
        ]
    return items


# ── Workflow status ───────────────────────────────────────────────

def build_workflow_status_entry(
    name: str,
    report: Optional[Dict[str, Any]],
    version_record: Optional[Dict[str, Any]] = None,
    live_state: Optional[str] = None,
    execution_count: int = 0,
) -> WorkflowStatusEntry:
    """`report` is workflow_validator's per-workflow dict (`.healthy`,
    `.errors`, `.built`, ...) — the same structural-validation report
    `/platform/workflows/{workflow}` already exposes; `version_record`
    is a version_registry entry (`.version`, `.signature`, ...).

    `live_state` is the actual running/idle/failed/paused signal. No
    module in the codebase this milestone was handed currently tracks
    live per-workflow execution state (workflow_validator only
    validates *graph structure*, not runtime), so this defaults to a
    conservative structural inference — "failed" if the graph itself
    doesn't validate/build, "idle" otherwise — and a caller that does
    have a real execution-state source (e.g. once Monitoring or a
    future scheduler tracks live runs) should pass it in via
    `live_state` to override this default outright."""
    report = report or {}
    healthy = bool(report.get("healthy", True))
    built = report.get("built", healthy)
    errors = list(report.get("errors") or [])

    if live_state is not None:
        state = live_state
    elif not built or not healthy:
        state = "failed"
    else:
        state = "idle"

    return WorkflowStatusEntry(
        name=name,
        state=state,
        healthy=healthy,
        version=(version_record or {}).get("version"),
        graph_hash=(version_record or {}).get("signature"),
        execution_count=execution_count,
        errors=errors,
    )


# ── Service status / health ───────────────────────────────────────

def build_service_status_list(health_report: Optional[Dict[str, Any]]) -> List[ServiceStatus]:
    """`health_report` is health_validator.generate_health_report's
    normalized dict. Two shapes are accepted so this survives either a
    richer future health report or today's simpler one without
    changing: a `services`/`components` list of {name, status, detail}
    dicts if present, else a single overall status is expanded to one
    row per known department name so the card still renders something
    meaningful."""
    if not health_report:
        return []

    rows = health_report.get("services") or health_report.get("components")
    if rows:
        return [
            ServiceStatus(name=r.get("name", "unknown"), status=r.get("status", "unknown"), detail=r.get("detail"))
            for r in rows
        ]

    overall = health_report.get("overall")
    overall_status = overall.get("value") if isinstance(overall, dict) else overall
    departments = health_report.get("departments") or []
    if not departments:
        return [ServiceStatus(name="platform", status=str(overall_status or "unknown"))]
    return [ServiceStatus(name=d, status=str(overall_status or "unknown")) for d in departments]


# ── Incidents ──────────────────────────────────────────────────────

def build_incident_summary(raw: Dict[str, Any]) -> IncidentSummary:
    return IncidentSummary(
        id=str(raw.get("id")),
        title=raw.get("title") or raw.get("event_type", "incident"),
        severity=raw.get("severity", "info"),
        status=raw.get("status", "open"),
        workflow=raw.get("workflow"),
        opened_at=raw.get("opened_at") or raw.get("recorded_at"),
        source=raw.get("source", "audit_trail"),
    )


def build_incident_list(raw_incidents: List[Dict[str, Any]]) -> List[IncidentSummary]:
    return [build_incident_summary(r) for r in raw_incidents]


# ── Versioning ─────────────────────────────────────────────────────

def build_version_summary(
    workflow: str,
    history: Optional[List[Dict[str, Any]]] = None,
    compatibility: Optional[Dict[str, Any]] = None,
) -> VersionSummary:
    """`history` is version_registry.list_versions(workflow) (oldest
    or newest first — this only needs the last two entries, however
    the caller's history happens to be ordered, so it takes them from
    whichever end is more recent by sorting isn't attempted here;
    dashboard_service.py is responsible for passing history already in
    the registry's own natural order with the most recent entry
    last)."""
    history = history or []
    current = history[-1] if history else None
    previous = history[-2] if len(history) >= 2 else None
    compatible = None
    if compatibility is not None:
        compatible = compatibility.get("compatible")
    return VersionSummary(
        workflow=workflow,
        current_version=(current or {}).get("version"),
        previous_version=(previous or {}).get("version"),
        is_breaking_from_previous=(current or {}).get("is_breaking_from_previous"),
        compatible_with_previous=compatible,
    )


# ── Metrics ────────────────────────────────────────────────────────

def build_metrics_snapshot(raw: Optional[Dict[str, Any]]) -> MetricsSnapshot:
    """`raw` is whatever the Monitoring Service's metrics call
    returns, normalized to a flat dict by dashboard_service.py. `raw
    is None` means Monitoring wasn't reachable/wired in this process —
    that degrades to `available=False` with a note, the same "missing
    infra is not an error" convention `/platform/traces` established,
    rather than raising."""
    if raw is None:
        return MetricsSnapshot(available=False, note="monitoring metrics unavailable in this process")

    known = {"workflow_latency_ms", "executions_total", "alerts_total", "deployments_total", "websocket_connections"}
    extra = {k: v for k, v in raw.items() if k not in known}
    return MetricsSnapshot(
        workflow_latency_ms=raw.get("workflow_latency_ms"),
        executions_total=raw.get("executions_total"),
        alerts_total=raw.get("alerts_total"),
        deployments_total=raw.get("deployments_total"),
        websocket_connections=raw.get("websocket_connections"),
        extra=extra,
        available=True,
    )


# ── Top-level summary + assembly ──────────────────────────────────

def build_dashboard_summary(
    services: List[ServiceStatus],
    workflows: List[WorkflowStatusEntry],
    incidents: List[IncidentSummary],
    recent_event_count: int,
    overall_ready: Optional[bool] = None,
    readiness_score: Optional[float] = None,
    health_status: str = "unknown",
    degraded_sections: Optional[List[str]] = None,
) -> DashboardSummary:
    return DashboardSummary(
        generated_at=_now_iso(),
        overall_ready=overall_ready,
        readiness_score=readiness_score,
        health_status=health_status,
        service_count=len(services),
        healthy_service_count=sum(1 for s in services if s.status == "healthy"),
        workflow_count=len(workflows),
        workflows_healthy_count=sum(1 for w in workflows if w.healthy),
        workflows_running_count=sum(1 for w in workflows if w.state == "running"),
        workflows_failed_count=sum(1 for w in workflows if w.state == "failed"),
        active_incident_count=sum(1 for i in incidents if i.status not in ("resolved", "closed")),
        recent_event_count=recent_event_count,
        degraded_sections=degraded_sections or [],
    )


def build_chaos_summary(raw: Optional[Dict[str, Any]]) -> Optional[ChaosSummary]:
    """M4.5 §12 — turns `chaos_dashboard.fetch_chaos_dashboard_section`'s
    raw dict into the pydantic card. `raw=None` (chaos tables/DB not
    wired, or the fetch itself failed) yields `None`, same "missing
    card, not a broken dashboard" convention every other `build_*`
    function in this file follows for an unavailable data source."""
    if raw is None:
        return None
    return ChaosSummary(
        running_scenarios=raw.get("running_scenarios", []),
        active_faults=raw.get("active_faults", []),
        latest_resilience_score=raw.get("latest_resilience_score"),
        historical_trend=raw.get("historical_trend", []),
    )


def build_security_summary(raw: Optional[Dict[str, Any]]) -> Optional[SecuritySummary]:
    """M4.6 Dashboard Integration — turns `security_repository.
    fetch_security_dashboard_section`'s raw dict into the pydantic
    card. `raw=None` (security tables/DB not wired, or the fetch
    itself failed) yields `None`, same \"missing card, not a broken
    dashboard\" convention `build_chaos_summary` follows for M4.5."""
    if raw is None:
        return None
    return SecuritySummary(
        latest_posture_score=raw.get("latest_posture_score"),
        latest_status=raw.get("latest_status"),
        active_finding_counts=raw.get("active_finding_counts", {}),
        historical_trend=raw.get("historical_trend", []),
    )


def build_plugins_summary(raw: Optional[Dict[str, Any]]) -> Optional[PluginsSummary]:
    """M4.7 Dashboard Integration — turns `plugin_repository.
    fetch_plugin_dashboard_section`'s raw dict into the pydantic card.
    `raw=None` (plugin tables/DB not wired, or the fetch itself
    failed) yields `None`, same \"missing card, not a broken dashboard\"
    convention `build_chaos_summary`/`build_security_summary` follow."""
    if raw is None:
        return None
    return PluginsSummary(
        installed_count=raw.get("installed_count", 0), enabled_count=raw.get("enabled_count", 0),
        disabled_count=raw.get("disabled_count", 0), error_count=raw.get("error_count", 0),
        unhealthy_plugins=raw.get("unhealthy_plugins", []),
    )


def build_designer_summary(raw: Optional[Dict[str, Any]]) -> Optional[DesignerSummary]:
    """M4.8 Dashboard Integration — turns `designer_repository.
    fetch_designer_dashboard_section`'s raw dict into the pydantic card.
    `raw=None` (designer tables/DB not wired, or the fetch itself
    failed) yields `None`, same "missing card, not a broken dashboard"
    convention `build_chaos_summary`/`build_security_summary`/
    `build_plugins_summary` follow."""
    if raw is None:
        return None
    return DesignerSummary(
        workflow_count=raw.get("workflow_count", 0), recent_edits=raw.get("recent_edits", []),
        invalid_count=raw.get("invalid_count", 0),
    )


def build_production_summary(raw: Optional[Dict[str, Any]]) -> Optional[ProductionSummary]:
    """M4.9 Dashboard Integration — turns `production_repository.
    fetch_production_dashboard_section`'s raw dict into the pydantic
    card. `raw=None` (production tables/DB not wired, or the fetch
    itself failed) yields `None`, same "missing card, not a broken
    dashboard" convention `build_chaos_summary`/`build_security_summary`/
    `build_plugins_summary`/`build_designer_summary` follow."""
    if raw is None:
        return None
    return ProductionSummary(
        latest_release_version=raw.get("latest_release_version"),
        latest_backup_at=raw.get("latest_backup_at"),
        backup_count=raw.get("backup_count", 0),
        latest_environment_status=raw.get("latest_environment_status"),
    )


def build_release_summary(raw: Optional[Dict[str, Any]]) -> Optional["ReleaseSummary"]:
    """M4.10 Dashboard Integration — turns a plain dict (as produced by
    `release_validation.readiness_report`/`chaos`/`security`/`plugins`/
    `designer`/`production` summaries already assembled elsewhere) into
    the Release card. `raw=None` yields `None`, same "missing card, not
    a broken dashboard" convention every prior M4.x summary builder in
    this module follows. Callers typically build `raw` by re-projecting
    fields already computed for the other cards in the same dashboard
    assembly call (see `dashboard_service.py`'s integration point) —
    this function does not itself recompute a readiness score."""
    if raw is None:
        return None
    return ReleaseSummary(
        readiness_score=raw.get("readiness_score"),
        workflow_count=raw.get("workflow_count", 0),
        plugin_count=raw.get("plugin_count", 0),
        security_posture=raw.get("security_posture"),
        chaos_score=raw.get("chaos_score"),
        production_score=raw.get("production_score"),
        version=raw.get("version"),
        git_commit=raw.get("git_commit"),
        build_date=raw.get("build_date"),
    )


def assemble_platform_dashboard(
    services: List[ServiceStatus],
    workflows: List[WorkflowStatusEntry],
    events: List[EventStreamItem],
    incidents: List[IncidentSummary],
    versions: List[VersionSummary],
    metrics: MetricsSnapshot,
    overall_ready: Optional[bool] = None,
    readiness_score: Optional[float] = None,
    health_status: str = "unknown",
    degraded_sections: Optional[List[str]] = None,
    chaos: Optional[ChaosSummary] = None,
    security: Optional[SecuritySummary] = None,
    plugins: Optional[PluginsSummary] = None,
    designer: Optional[DesignerSummary] = None,
    production: Optional[ProductionSummary] = None,
    release: Optional[ReleaseSummary] = None,
) -> PlatformDashboard:
    degraded_sections = degraded_sections or []
    summary = build_dashboard_summary(
        services, workflows, incidents, len(events),
        overall_ready=overall_ready, readiness_score=readiness_score,
        health_status=health_status, degraded_sections=degraded_sections,
    )
    return PlatformDashboard(
        generated_at=summary.generated_at,
        summary=summary,
        services=services,
        workflows=workflows,
        events=events,
        incidents=incidents,
        versions=versions,
        metrics=metrics,
        chaos=chaos,
        security=security,
        plugins=plugins,
        designer=designer,
        production=production,
        release=release,
        degraded_sections=degraded_sections,
    )
