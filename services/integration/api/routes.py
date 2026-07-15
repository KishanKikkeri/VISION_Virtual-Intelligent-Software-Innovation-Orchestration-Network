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

from typing import Any, Dict

import structlog
from fastapi import APIRouter, Request

from services.integration import artifact_validator, dependency_graph, event_router, lifecycle
from services.integration.health_validator import generate_health_report
from services.integration.orchestrator import compute_readiness, generate_full_report, validate_agent_registry
from services.integration.repository import (
    DependencyCheckRepository, PlatformReportRepository, ValidationResultRepository,
)

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
    results = lifecycle.validate_all_workflows()
    return {name: r.model_dump(mode="json") for name, r in results.items()}


@router.get("/registry")
async def platform_registry() -> Dict[str, Any]:
    return validate_agent_registry().model_dump(mode="json")


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
