"""
services/integration/api/routes.py
=================================
The 7 required platform APIs (spec "Required APIs"):
    GET /platform/health
    GET /platform/readiness
    GET /platform/dependencies
    GET /platform/events
    GET /platform/workflows
    GET /platform/registry
    GET /platform/report

Each of the first 6 exposes exactly one validator's output directly.
`/platform/report` runs the full orchestrator, persists it (best
effort — never blocks the response on a DB write failure), and returns
it — this is the one endpoint that both reads and writes.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import structlog
from fastapi import APIRouter, HTTPException, Request

from infrastructure.database.repositories import ArtifactRepository, AuditRepository
from services.integration import artifact_validator, dependency_graph, event_router, lifecycle
from services.integration.diagnostics import graph_exporter
from services.integration.health_validator import generate_health_report
from services.integration.orchestrator import compute_readiness, generate_full_report, validate_agent_registry
from services.integration.repository import (
    DependencyCheckRepository, PlatformReportRepository, ValidationResultRepository, WorkflowVersionRepository,
)
from services.integration.validators import workflow_validator
from services.integration.versioning import compatibility_checker
from services.integration.versioning.version_registry import default_registry as version_registry
from services.integration.versioning.version_registry import register_current_version
from services.integration.replay import artifact_diff as artifact_diff_module
from services.integration.replay import execution_timeline as execution_timeline_module
from services.integration.replay import replay_engine as replay_engine_module

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/platform", tags=["Platform Integration"])


def _infra(request: Request) -> Dict[str, Any]:
    """Pulls db_factory/nats/factory off app.state if main.py's lifespan
    populated them; every field defaults to None so this router also
    works against a bare FastAPI app in tests."""
    state = request.app.state
    return {
        "db_factory": getattr(state, "db_factory", None),
        "nats": getattr(state, "nats", None),
        "factory": getattr(state, "agent_factory", None),
    }


@router.get("/health")
async def platform_health(request: Request) -> Dict[str, Any]:
    infra = _infra(request)
    report = await generate_health_report(**infra)
    return report.model_dump(mode="json")


@router.get("/readiness")
async def platform_readiness(request: Request) -> Dict[str, Any]:
    infra = _infra(request)
    full = await generate_full_report(**infra)
    return full.readiness.model_dump(mode="json")


@router.get("/dependencies")
async def platform_dependencies() -> Dict[str, Any]:
    baseline = dependency_graph.full_lifecycle_report(set())
    return {
        "phase_tiers": dependency_graph.PHASE_TIERS,
        "dependencies": dependency_graph.DEPARTMENT_DEPENDENCIES,
        "orphan_departments": sorted(dependency_graph.ORPHAN_DEPARTMENTS),
        "has_cycle": dependency_graph.has_cycle(),
        "baseline_checks": {k: v.model_dump(mode="json") for k, v in baseline.items()},
    }


@router.get("/events")
async def platform_events() -> Dict[str, Any]:
    report = event_router.generate_event_report()
    return report.model_dump(mode="json")


@router.get("/workflows")
async def platform_workflows() -> Dict[str, Any]:
    """M3.10: now backed by the richer workflow_validator.WorkflowReport
    (node/edge counts, interrupts, parallel branches, cycles, etc.)
    rather than lifecycle.py's lighter GraphAnalysis. Still a dict keyed
    by workflow name, so existing callers reading `response["qa"]`
    continue to work; the *shape* of each value is richer than before."""
    results = workflow_validator.validate_all_workflows_detailed()
    return {name: r.model_dump(mode="json") for name, r in results.items()}


@router.get("/workflows/validate")
async def platform_workflows_validate() -> Dict[str, Any]:
    """Aggregate pass/fail summary across all 10 workflows — the
    single-glance version of GET /platform/workflows."""
    results = workflow_validator.validate_all_workflows_detailed()
    healthy = {name: r.healthy for name, r in results.items()}
    return {
        "all_healthy": all(healthy.values()),
        "healthy_count": sum(1 for v in healthy.values() if v),
        "total": len(healthy),
        "workflows": healthy,
        "errors": {name: r.errors for name, r in results.items() if r.errors},
        "warnings": {name: r.warnings for name, r in results.items() if r.warnings},
    }


@router.get("/workflows/mermaid/{workflow}")
async def platform_workflows_mermaid(workflow: str) -> Dict[str, Any]:
    nodes, _edges, error = graph_exporter.build_graph_edges(workflow)
    if error and not nodes:
        raise HTTPException(status_code=404, detail=f"unknown or unbuildable workflow {workflow!r}: {error}")
    return {"workflow": workflow, "mermaid": graph_exporter.generate_mermaid(workflow)}


@router.get("/workflows/{workflow}")
async def platform_workflow_detail(workflow: str) -> Dict[str, Any]:
    report = workflow_validator.get_workflow_report(workflow)
    if report is None:
        raise HTTPException(status_code=404, detail=f"unknown workflow {workflow!r}")
    return report.model_dump(mode="json")


@router.get("/registry")
async def platform_registry() -> Dict[str, Any]:
    return validate_agent_registry().model_dump(mode="json")


@router.get("/traces/{project_id}")
async def platform_traces(project_id: str, request: Request, limit: int = 100) -> Dict[str, Any]:
    """Developer Experience — execution traces for the workflow explorer.
    Backed by the real AuditEvent append-only log (infrastructure/database/
    models.py: "the black box recorder"), not synthetic data — a project
    with no recorded audit events simply gets an empty `events` list back,
    not a 404 (a project can genuinely have no activity yet)."""
    db_factory = _infra(request).get("db_factory")
    if db_factory is None:
        return {"project_id": project_id, "events": [], "note": "no database configured in this process"}

    try:
        async with db_factory() as db:
            rows = await AuditRepository.list_for_project(db, project_id, limit=limit)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"could not read audit trail: {e}")

    return {
        "project_id": project_id,
        "count": len(rows),
        "events": [
            {
                "id": r.id, "event_type": r.event_type, "actor_type": r.actor_type, "actor_id": r.actor_id,
                "entity_type": r.entity_type, "entity_id": r.entity_id, "payload": r.payload,
                "recorded_at": r.recorded_at.isoformat() if r.recorded_at else None,
            }
            for r in rows
        ],
    }


@router.get("/report")
async def platform_report(request: Request) -> Dict[str, Any]:
    infra = _infra(request)
    full = await generate_full_report(**infra)

    db_factory = infra.get("db_factory")
    if db_factory is not None:
        try:
            async with db_factory() as db:
                row = await PlatformReportRepository.record(
                    db, full.readiness.overall,
                    {c.name: c.score for c in full.readiness.categories},
                    full.health.overall.value,
                    summary=f"{len(full.workflows)} workflows, "
                            f"{full.events.total_subjects} NATS subjects, "
                            f"{full.registry.total_agents} agents.",
                )
                for name, w in full.workflows.items():
                    await ValidationResultRepository.record(db, row.id, f"workflow:{name}", w.passed,
                                                              w.model_dump(mode="json"))
                for dept, check in full.dependency_sample.items():
                    await DependencyCheckRepository.record(db, row.id, dept, check.passed,
                                                             {"missing": check.missing})
        except Exception as e:
            log.warning("platform_report_persist_failed", error=str(e))

    return full.model_dump(mode="json")


# ── M4.1 Workflow Versioning ──────────────────────────────────────
# Sub-paths of /workflows/{workflow} — safe against the existing
# catch-all (services/integration/api/routes.py's
# platform_workflow_detail) since FastAPI matches on path *shape*, not
# just prefix: these all have more path segments than the two-segment
# catch-all, so registration order relative to it doesn't matter. The
# one ordering rule that does matter (per the M3.10 precedent already
# in this file) is that a static-looking segment like "register" must
# never be swallowed by a `{version}` path parameter — so /register is
# a POST (different method, no ambiguity) and is defined above the
# GET .../versions/{version} route regardless.

def _require_known_workflow(workflow: str) -> None:
    if workflow_validator.get_workflow_report(workflow) is None:
        raise HTTPException(status_code=404, detail=f"unknown workflow {workflow!r}")


@router.get("/workflows/{workflow}/versions")
async def platform_workflow_versions(workflow: str, request: Request) -> Dict[str, Any]:
    """Ensures the workflow's *current* structural shape is registered
    (idempotent — a no-op if nothing changed since the last call, per
    version_registry.VersionRegistry.register), then returns the full
    version history. Also persists any newly-created version to the DB,
    best-effort, exactly like /platform/report's persistence path."""
    _require_known_workflow(workflow)
    try:
        current = register_current_version(workflow)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    await _persist_version_if_new(request, current)

    history = version_registry.list_versions(workflow)
    return {
        "workflow": workflow,
        "latest_version": current.version,
        "count": len(history),
        "versions": [v.model_dump(mode="json") for v in history],
    }


@router.post("/workflows/{workflow}/versions/register")
async def platform_workflow_versions_register(workflow: str, request: Request) -> Dict[str, Any]:
    """Explicit registration action — same effect as GET .../versions'
    side effect, exposed as its own endpoint for callers (e.g. a CI
    step run after a workflow-graph change) that want to register a
    version without also paying for serializing the full history back."""
    _require_known_workflow(workflow)
    try:
        current = register_current_version(workflow)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    persisted = await _persist_version_if_new(request, current)
    return {"workflow": workflow, "version": current.version, "persisted": persisted,
            "record": current.model_dump(mode="json")}


@router.get("/workflows/{workflow}/versions/{version}")
async def platform_workflow_version_detail(workflow: str, version: str) -> Dict[str, Any]:
    _require_known_workflow(workflow)
    record = version_registry.get(workflow, version)
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown version {version!r} for workflow {workflow!r}")
    return record.model_dump(mode="json")


@router.get("/workflows/{workflow}/diff")
async def platform_workflow_diff(workflow: str, from_version: str, to_version: str) -> Dict[str, Any]:
    _require_known_workflow(workflow)
    diff = version_registry.diff_versions(workflow, from_version, to_version)
    if diff is None:
        raise HTTPException(
            status_code=404,
            detail=f"one or both versions ({from_version!r}, {to_version!r}) not found for workflow {workflow!r}")
    return {"workflow": workflow, "from_version": from_version, "to_version": to_version,
            "diff": diff.model_dump(mode="json")}


@router.get("/workflows/{workflow}/compatibility")
async def platform_workflow_compatibility(
    workflow: str, from_version: str, to_version: str, node: Optional[str] = None,
) -> Dict[str, Any]:
    """Answers: can a checkpoint paused at `node` (optional — omit for
    the conservative whole-graph check) under `from_version` safely
    resume against `to_version`? See compatibility_checker.py."""
    _require_known_workflow(workflow)
    result = compatibility_checker.check_compatibility(workflow, from_version, to_version, checkpoint_node=node)
    return result.model_dump(mode="json")


async def _persist_version_if_new(request: Request, record) -> bool:
    """Best-effort DB persistence: only writes a row if this exact
    (workflow, version) isn't already there, so re-registering an
    unchanged graph doesn't churn the append-only table. Never raises —
    a DB failure here must not break the versioning read path, mirroring
    every other best-effort persistence call in this router."""
    db_factory = _infra(request).get("db_factory")
    if db_factory is None:
        return False
    try:
        async with db_factory() as db:
            existing = await WorkflowVersionRepository.get(db, record.workflow, record.version)
            if existing is not None:
                return False
            await WorkflowVersionRepository.record(
                db, record.workflow, record.version, record.signature,
                record.nodes, record.edges, record.routes,
                record.is_breaking_from_previous,
                record.diff_from_previous.model_dump(mode="json") if record.diff_from_previous else None,
                notes=record.notes,
            )
            return True
    except Exception as e:  # noqa: BLE001
        log.warning("workflow_version_persist_failed", workflow=record.workflow, version=record.version,
                    error=str(e))
        return False


# ── M4.2 Execution Replay ─────────────────────────────────────────
# All backed by the real AuditEvent trail and the real artifacts
# table — nothing here is synthetic. A project/artifact with no
# recorded history gets an empty/zero-count response, not a 404 (same
# "no activity yet is not an error" convention /platform/traces
# already uses), except where a specific artifact_type/version pair
# was asked for by name and genuinely doesn't exist, which is a 404.

@router.get("/replay/{project_id}/timeline")
async def platform_replay_timeline(project_id: str, request: Request, limit: int = 1000) -> Dict[str, Any]:
    db_factory = _infra(request).get("db_factory")
    if db_factory is None:
        return {"project_id": project_id, "event_count": 0, "note": "no database configured in this process"}

    try:
        async with db_factory() as db:
            timeline = await execution_timeline_module.get_execution_timeline(db, project_id, limit=limit)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"could not read audit trail: {e}")

    return timeline.model_dump(mode="json")


@router.get("/replay/{project_id}/trace")
async def platform_replay_trace(project_id: str, request: Request, limit: int = 1000) -> Dict[str, Any]:
    """The audit-trail-sourced replay trace (see replay_engine.py's
    docstring for why this, not a checkpoint-sourced trace, is what's
    exposed over HTTP today: no workflow in this codebase is compiled
    against a live checkpointer yet, so there is nothing for a
    checkpoint-sourced /trace endpoint to read)."""
    db_factory = _infra(request).get("db_factory")
    if db_factory is None:
        return {"project_id": project_id, "step_count": 0, "note": "no database configured in this process"}

    try:
        async with db_factory() as db:
            trace = await replay_engine_module.get_audit_trail_trace(db, project_id, limit=limit)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"could not read audit trail: {e}")

    return trace.model_dump(mode="json")


@router.get("/replay/{project_id}/state/{step}")
async def platform_replay_state_at_step(project_id: str, step: int, request: Request,
                                         limit: int = 1000) -> Dict[str, Any]:
    """Time-travel: the reconstructed state at one specific step of the
    project's audit trail, without needing to fetch or replay the
    whole trace client-side."""
    db_factory = _infra(request).get("db_factory")
    if db_factory is None:
        raise HTTPException(status_code=503, detail="no database configured in this process")

    try:
        async with db_factory() as db:
            trace = await replay_engine_module.get_audit_trail_trace(db, project_id, limit=limit)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"could not read audit trail: {e}")

    state = replay_engine_module.state_at_step(trace, step)
    if state is None:
        raise HTTPException(status_code=404, detail=f"step {step} out of range (trace has {trace.step_count} steps)")
    return {"project_id": project_id, "step": step, "state": state}


@router.get("/artifacts/{project_id}/{artifact_type}/versions")
async def platform_artifact_versions(project_id: str, artifact_type: str, request: Request) -> Dict[str, Any]:
    db_factory = _infra(request).get("db_factory")
    if db_factory is None:
        return {"project_id": project_id, "artifact_type": artifact_type, "count": 0,
                "note": "no database configured in this process"}

    try:
        async with db_factory() as db:
            rows = await ArtifactRepository.list_versions_for_type(db, project_id, artifact_type)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"could not read artifacts: {e}")

    return {
        "project_id": project_id, "artifact_type": artifact_type, "count": len(rows),
        "versions": [
            {"version": r.version, "status": r.status, "created_by": r.created_by, "approved_by": r.approved_by,
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rows
        ],
    }


@router.get("/artifacts/{project_id}/{artifact_type}/diff")
async def platform_artifact_diff(
    project_id: str, artifact_type: str, from_version: int, to_version: int, request: Request,
) -> Dict[str, Any]:
    db_factory = _infra(request).get("db_factory")
    if db_factory is None:
        raise HTTPException(status_code=503, detail="no database configured in this process")

    try:
        async with db_factory() as db:
            diff = await artifact_diff_module.diff_artifact_versions(
                db, project_id, artifact_type, from_version, to_version)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"could not read artifacts: {e}")

    if diff is None:
        raise HTTPException(
            status_code=404,
            detail=f"version {from_version} or {to_version} not found for {artifact_type!r} in project {project_id!r}")
    return diff.model_dump(mode="json")
