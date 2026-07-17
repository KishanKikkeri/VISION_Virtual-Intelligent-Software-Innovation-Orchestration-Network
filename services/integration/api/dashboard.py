"""
services/integration/api/dashboard.py
=================================
M4.3 — Live Operations Dashboard API. Seven endpoints, all read-only:

    GET /platform/dashboard
    GET /platform/dashboard/summary
    GET /platform/dashboard/events
    GET /platform/dashboard/services
    GET /platform/dashboard/workflows
    GET /platform/dashboard/metrics
    GET /platform/dashboard/incidents

Plus one small integration convenience:

    GET /platform/dashboard/replay-links/{project_id}

...which does not duplicate M4.2's replay logic — it returns the same
`/platform/replay/{project_id}/*` and `/platform/artifacts/*` paths
`services/integration/api/routes.py` already serves, so a UI card can
build a "open in replay" link without hand-assembling the URL scheme
itself. Per the M4.3 handover's "Replay Integration" and "Version
Integration" requirements, and the constraint against rewriting the
Replay Engine or Version Registry, every other endpoint here is a thin
wrapper around `dashboard_service.build_platform_dashboard`, which is
itself a thin orchestrator over the platform's *existing* modules (see
that file's docstring) — nothing in this router computes health,
readiness, or workflow validity itself.

Registered separately from `services/integration/api/routes.py`'s
router (own `APIRouter`, same `/platform` prefix) rather than added
into that file directly, matching this milestone's "new services /
new endpoints, no rewrites" constraint — `main.py` needs one additional
`app.include_router(dashboard_router)` alongside the existing one.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import structlog
from fastapi import APIRouter, Request

from services.integration.dashboard import dashboard_service
from services.integration.dashboard.dashboard_cache import default_cache

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/platform/dashboard", tags=["Live Operations Dashboard"])


def _infra(request: Request) -> Dict[str, Any]:
    """Mirrors services/integration/api/routes.py's `_infra` helper
    (kept as a small local copy rather than importing that module, so
    this router — and its tests — don't take on every dependency
    routes.py happens to import at module load time just to read one
    attribute off app.state)."""
    state = request.app.state
    return {
        "db_factory": getattr(state, "db_factory", None),
        "nats": getattr(state, "nats", None),
        "factory": getattr(state, "agent_factory", None),
    }


async def _open_db(request: Request):
    """Returns an already-open db session for this request, or None if
    no db_factory is configured — every route below tolerates None
    (dashboard_service degrades the relevant cards rather than 500ing,
    same "no database configured in this process" convention
    /platform/traces and /platform/replay/* already use)."""
    db_factory = _infra(request).get("db_factory")
    if db_factory is None:
        return None, None
    ctx = db_factory()
    db = await ctx.__aenter__()
    return db, ctx


async def _close_db(ctx: Any) -> None:
    if ctx is not None:
        try:
            await ctx.__aexit__(None, None, None)
        except Exception as e:  # noqa: BLE001 — closing the session must never break the response
            log.warning("dashboard_db_close_failed", error=str(e))


@router.get("")
async def platform_dashboard(
    request: Request, event_limit: int = 200, category: Optional[str] = None,
    severity: Optional[str] = None, search: Optional[str] = None,
) -> Dict[str, Any]:
    db, ctx = await _open_db(request)
    try:
        dashboard = await dashboard_service.build_platform_dashboard(
            db=db, cache=default_cache, event_limit=event_limit,
            event_category=category, event_severity=severity, event_search=search,
        )
    finally:
        await _close_db(ctx)
    return dashboard.model_dump(mode="json")


@router.get("/summary")
async def platform_dashboard_summary(request: Request) -> Dict[str, Any]:
    db, ctx = await _open_db(request)
    try:
        dashboard = await dashboard_service.build_platform_dashboard(db=db, cache=default_cache)
    finally:
        await _close_db(ctx)
    return dashboard.summary.model_dump(mode="json")


@router.get("/services")
async def platform_dashboard_services(request: Request) -> Dict[str, Any]:
    db, ctx = await _open_db(request)
    try:
        dashboard = await dashboard_service.build_platform_dashboard(db=db, cache=default_cache)
    finally:
        await _close_db(ctx)
    return {"count": len(dashboard.services), "services": [s.model_dump(mode="json") for s in dashboard.services]}


@router.get("/workflows")
async def platform_dashboard_workflows(request: Request) -> Dict[str, Any]:
    db, ctx = await _open_db(request)
    try:
        dashboard = await dashboard_service.build_platform_dashboard(db=db, cache=default_cache)
    finally:
        await _close_db(ctx)
    return {"count": len(dashboard.workflows), "workflows": [w.model_dump(mode="json") for w in dashboard.workflows]}


@router.get("/events")
async def platform_dashboard_events(
    request: Request, limit: int = 200, category: Optional[str] = None,
    severity: Optional[str] = None, search: Optional[str] = None,
) -> Dict[str, Any]:
    db, ctx = await _open_db(request)
    try:
        dashboard = await dashboard_service.build_platform_dashboard(
            db=db, cache=default_cache, event_limit=limit,
            event_category=category, event_severity=severity, event_search=search,
        )
    finally:
        await _close_db(ctx)
    return {"count": len(dashboard.events), "events": [e.model_dump(mode="json") for e in dashboard.events]}


@router.get("/metrics")
async def platform_dashboard_metrics(request: Request) -> Dict[str, Any]:
    db, ctx = await _open_db(request)
    try:
        dashboard = await dashboard_service.build_platform_dashboard(db=db, cache=default_cache)
    finally:
        await _close_db(ctx)
    return dashboard.metrics.model_dump(mode="json")


@router.get("/incidents")
async def platform_dashboard_incidents(request: Request) -> Dict[str, Any]:
    db, ctx = await _open_db(request)
    try:
        dashboard = await dashboard_service.build_platform_dashboard(db=db, cache=default_cache)
    finally:
        await _close_db(ctx)
    return {"count": len(dashboard.incidents), "incidents": [i.model_dump(mode="json") for i in dashboard.incidents]}


@router.get("/replay-links/{project_id}")
async def platform_dashboard_replay_links(project_id: str) -> Dict[str, Any]:
    """Points at M4.2's real endpoints — does not read or compute
    anything itself, purely a URL-building convenience for the
    dashboard UI's "open in replay" affordance."""
    return {
        "project_id": project_id,
        "timeline": f"/platform/replay/{project_id}/timeline",
        "trace": f"/platform/replay/{project_id}/trace",
        "state_at_step": f"/platform/replay/{project_id}/state/{{step}}",
        "artifacts": f"/platform/artifacts/{project_id}/{{artifact_type}}/versions",
    }
