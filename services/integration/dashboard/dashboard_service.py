"""
services/integration/dashboard/dashboard_service.py
=================================
M4.3 §3 Dashboard Service — the orchestration layer. Calls the
platform's existing modules (health_validator, orchestrator,
workflow_validator, the M4.1 version registry, a Monitoring metrics
source, and dashboard_repository for the audit trail), normalizes each
one's output to the plain dicts/lists `dashboard_builder.py`'s pure
functions expect, and returns an assembled `PlatformDashboard`.

Every external call is isolated in its own `_fetch_*` function and
wrapped so a missing/unwired dependency degrades that one card rather
than failing the whole dashboard — the same "no activity yet / no
infra configured is not an error" convention `/platform/traces` and
this milestone's own replay endpoints already established, extended
here to "a department module isn't reachable from this process either
is not a 500". Each `_fetch_*` is deliberately a separate,
monkeypatchable module-level function (not an inline lambda) so tests
can override exactly one data source at a time without needing the
real department modules importable, mirroring how
`test_m42_execution_replay.py` monkeypatches
`services.integration.api.routes.execution_timeline_module.
get_execution_timeline`.

Results (other than the live event stream, which is deliberately
fresher) are cached briefly via `dashboard_cache.DashboardCache` so a
UI polling every few seconds doesn't re-run a full readiness
computation and workflow-graph validation on every single request —
see that module's docstring.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog

from services.integration.dashboard import dashboard_builder as builder
from services.integration.dashboard.dashboard_cache import DashboardCache, default_cache
from services.integration.dashboard.dashboard_models import (
    EventStreamItem, IncidentSummary, MetricsSnapshot, PlatformDashboard, ServiceStatus,
    VersionSummary, WorkflowStatusEntry,
)

log = structlog.get_logger(__name__)

DEFAULT_TTL_SECONDS = 3.0
_EVENTS_TTL_SECONDS = 2.0   # live feed: kept fresher than the rest
_VERSIONS_TTL_SECONDS = 15.0  # version history changes far less often


# ── Individual data sources (each independently degradable/mockable) ──

async def _fetch_health(**infra: Any) -> Optional[Dict[str, Any]]:
    try:
        from services.integration.health_validator import generate_health_report
        report = await generate_health_report(**infra)
        return report.model_dump(mode="json")
    except Exception as e:  # noqa: BLE001 — a missing/broken health source degrades this card, not the dashboard
        log.info("dashboard_health_unavailable", error=str(e))
        return None


async def _fetch_readiness(**infra: Any) -> Optional[Dict[str, Any]]:
    try:
        from services.integration.orchestrator import generate_full_report
        full = await generate_full_report(**infra)
        return full.readiness.model_dump(mode="json")
    except Exception as e:  # noqa: BLE001
        log.info("dashboard_readiness_unavailable", error=str(e))
        return None


async def _fetch_workflow_reports() -> Optional[Dict[str, Dict[str, Any]]]:
    try:
        from services.integration.validators import workflow_validator
        results = workflow_validator.validate_all_workflows_detailed()
        return {name: r.model_dump(mode="json") for name, r in results.items()}
    except Exception as e:  # noqa: BLE001
        log.info("dashboard_workflows_unavailable", error=str(e))
        return None


async def _fetch_version_history(workflow: str) -> Optional[List[Dict[str, Any]]]:
    """Read-only: unlike `/platform/workflows/{workflow}/versions`,
    this does NOT call `register_current_version` on every dashboard
    poll (that would churn a best-effort DB write path on every few-
    second refresh). It only registers if the registry has no history
    at all yet for this workflow, so a brand-new workflow still shows
    up with at least one version instead of an empty card."""
    try:
        from services.integration.versioning.version_registry import default_registry, register_current_version
        history = default_registry.list_versions(workflow)
        if not history:
            try:
                register_current_version(workflow, registry=default_registry)
                history = default_registry.list_versions(workflow)
            except Exception:  # noqa: BLE001 — fine, just means no version to show yet
                pass
        return [v.model_dump(mode="json") for v in history]
    except Exception as e:  # noqa: BLE001
        log.info("dashboard_version_history_unavailable", workflow=workflow, error=str(e))
        return None


async def _fetch_compatibility(workflow: str, from_version: str, to_version: str) -> Optional[Dict[str, Any]]:
    try:
        from services.integration.versioning import compatibility_checker
        result = compatibility_checker.check_compatibility(workflow, from_version, to_version)
        return result.model_dump(mode="json")
    except Exception as e:  # noqa: BLE001
        log.info("dashboard_compatibility_unavailable", workflow=workflow, error=str(e))
        return None


async def _fetch_metrics(**infra: Any) -> Optional[Dict[str, Any]]:
    """No single confirmed entry point for Monitoring Service metrics
    was available from where M4.3 was implemented, so this tries the
    two most likely shapes a Monitoring module in this codebase would
    expose, in order, and degrades to unavailable rather than guessing
    further. Whichever one is real, only this function needs to change
    — `dashboard_builder.build_metrics_snapshot` and everything
    downstream already accept `None` for "not available"."""
    candidates = (
        ("services.monitoring.metrics_service", "get_platform_metrics"),
        ("services.monitoring.dashboard_metrics", "get_dashboard_metrics"),
    )
    for module_path, func_name in candidates:
        try:
            module = __import__(module_path, fromlist=[func_name])
            func = getattr(module, func_name)
            result = func(**infra) if not _is_coro_func(func) else await func(**infra)
            return dict(result) if not hasattr(result, "model_dump") else result.model_dump(mode="json")
        except Exception:  # noqa: BLE001 — try the next candidate
            continue
    log.info("dashboard_metrics_unavailable", tried=[c[0] for c in candidates])
    return None


def _is_coro_func(func: Any) -> bool:
    import inspect
    return inspect.iscoroutinefunction(func)


async def _fetch_events(db: Any, limit: int = 200, category: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    if db is None:
        return None
    try:
        from services.integration.dashboard import dashboard_repository
        return await dashboard_repository.get_recent_events(db, limit=limit, category=category)
    except Exception as e:  # noqa: BLE001
        log.info("dashboard_events_unavailable", error=str(e))
        return None


async def _fetch_incidents(db: Any, limit: int = 100) -> Optional[List[Dict[str, Any]]]:
    if db is None:
        return None
    try:
        from services.integration.dashboard import dashboard_repository
        return await dashboard_repository.get_incidents(db, limit=limit)
    except Exception as e:  # noqa: BLE001
        log.info("dashboard_incidents_unavailable", error=str(e))
        return None


async def _fetch_chaos(db: Any) -> Optional[Dict[str, Any]]:
    """M4.5 §12 Dashboard Integration. `db=None` degrades to
    in-memory-only (currently-running scenarios still shown; no
    resilience-score history) rather than `None` outright — chaos runs
    are meaningful to show even without a DB configured, unlike
    sections that are pure DB reads. See `chaos_dashboard.
    fetch_chaos_dashboard_section`'s own docstring for the `None`
    contract this wraps."""
    try:
        from services.integration.chaos import chaos_dashboard
        return await chaos_dashboard.fetch_chaos_dashboard_section(db)
    except Exception as e:  # noqa: BLE001
        log.info("dashboard_chaos_unavailable", error=str(e))
        return None


# ── Orchestration ──────────────────────────────────────────────────

async def _gather_raw(
    db: Any, event_limit: int, event_category: Optional[str], cache: DashboardCache,
) -> Dict[str, Any]:
    """Fetches (or serves from cache) every raw section this dashboard
    needs. Kept as one function so callers needing only part of the
    dashboard (e.g. `/dashboard/workflows`) still benefit from the same
    cache entries a full `/dashboard` fetch already warmed."""
    health = await cache.get_or_build("health", lambda: _fetch_health(db_factory=None), DEFAULT_TTL_SECONDS) \
        if db is None else await cache.get_or_build(
            "health", lambda: _fetch_health(db_factory=lambda: _ConstDbCtx(db)), DEFAULT_TTL_SECONDS)
    readiness = await cache.get_or_build("readiness", lambda: _fetch_readiness(), DEFAULT_TTL_SECONDS)
    workflow_reports = await cache.get_or_build("workflow_reports", _fetch_workflow_reports, DEFAULT_TTL_SECONDS)

    version_histories: Dict[str, Optional[List[Dict[str, Any]]]] = {}
    for name in (workflow_reports or {}):
        version_histories[name] = await cache.get_or_build(
            f"version_history:{name}", (lambda n=name: _fetch_version_history(n)), _VERSIONS_TTL_SECONDS)

    metrics = await cache.get_or_build("metrics", _fetch_metrics, DEFAULT_TTL_SECONDS)
    events = await _fetch_events(db, limit=event_limit, category=event_category)  # always fresh: it's the live feed
    incidents = await cache.get_or_build("incidents", lambda: _fetch_incidents(db), DEFAULT_TTL_SECONDS)
    chaos = await cache.get_or_build("chaos", lambda: _fetch_chaos(db), DEFAULT_TTL_SECONDS)

    return {
        "health": health, "readiness": readiness, "workflow_reports": workflow_reports,
        "version_histories": version_histories, "metrics": metrics, "events": events, "incidents": incidents,
        "chaos": chaos,
    }


class _ConstDbCtx:
    """Tiny async-context-manager shim so `_fetch_health`/`_fetch_
    readiness` (which expect a `db_factory` callable per the rest of
    this codebase's `_infra()` convention) can be handed an
    already-open `db` session from the dashboard route without every
    department module needing a second calling convention."""
    def __init__(self, db: Any) -> None:
        self._db = db

    async def __aenter__(self) -> Any:
        return self._db

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _degraded_sections(raw: Dict[str, Any]) -> List[str]:
    sections = []
    if raw.get("health") is None:
        sections.append("health")
    if raw.get("readiness") is None:
        sections.append("readiness")
    if raw.get("workflow_reports") is None:
        sections.append("workflows")
    if raw.get("metrics") is None:
        sections.append("metrics")
    if raw.get("events") is None:
        sections.append("events")
    if raw.get("incidents") is None:
        sections.append("incidents")
    if raw.get("chaos") is None:
        sections.append("chaos")
    return sections


def _build_from_raw(
    raw: Dict[str, Any],
    event_category: Optional[str] = None,
    event_severity: Optional[str] = None,
    event_search: Optional[str] = None,
) -> PlatformDashboard:
    workflow_reports = raw.get("workflow_reports") or {}
    version_histories = raw.get("version_histories") or {}

    workflows: List[WorkflowStatusEntry] = []
    versions: List[VersionSummary] = []
    for name, report in workflow_reports.items():
        history = version_histories.get(name) or []
        current = history[-1] if history else None
        workflows.append(builder.build_workflow_status_entry(name, report, version_record=current))
        versions.append(builder.build_version_summary(name, history=history))

    services: List[ServiceStatus] = builder.build_service_status_list(raw.get("health"))
    events: List[EventStreamItem] = builder.build_event_stream(
        raw.get("events") or [], category=event_category, severity=event_severity, search=event_search)
    incidents: List[IncidentSummary] = builder.build_incident_list(raw.get("incidents") or [])
    metrics: MetricsSnapshot = builder.build_metrics_snapshot(raw.get("metrics"))
    chaos = builder.build_chaos_summary(raw.get("chaos"))

    readiness = raw.get("readiness") or {}
    health = raw.get("health") or {}
    overall_status = health.get("overall")
    overall_status = overall_status.get("value") if isinstance(overall_status, dict) else overall_status

    return builder.assemble_platform_dashboard(
        services, workflows, events, incidents, versions, metrics,
        overall_ready=readiness.get("overall"),
        readiness_score=readiness.get("score"),
        health_status=str(overall_status or "unknown"),
        degraded_sections=_degraded_sections(raw),
        chaos=chaos,
    )


async def build_platform_dashboard(
    db: Any = None,
    cache: DashboardCache = default_cache,
    event_limit: int = 200,
    event_category: Optional[str] = None,
    event_severity: Optional[str] = None,
    event_search: Optional[str] = None,
) -> PlatformDashboard:
    """The single entry point every `/platform/dashboard*` route calls.
    `db` is an already-open DB session/connection for this request (the
    route opens it via `db_factory`, same as every other endpoint in
    `services/integration/api/routes.py`) — this function does not
    manage its own connection lifecycle."""
    raw = await _gather_raw(db, event_limit, event_category, cache)
    return _build_from_raw(raw, event_category=event_category, event_severity=event_severity, event_search=event_search)
