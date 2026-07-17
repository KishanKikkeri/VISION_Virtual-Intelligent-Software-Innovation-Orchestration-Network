"""
services/integration/startup.py
=================================
Spec §5 Startup Validator — run once when the platform (or this
integration service specifically) boots. Fails fast (raises
StartupValidationError) if required infrastructure is missing, unless
`strict=False` is passed (used by tests and by /platform/report,
which must be able to render a report *about* a broken platform
without itself refusing to start).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog
from pydantic import BaseModel, Field

from core.config.settings import get_settings
from core.runtime.factory import AGENT_REGISTRY

log = structlog.get_logger(__name__)


class StartupCheck(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class StartupReport(BaseModel):
    passed: bool
    checks: List[StartupCheck] = Field(default_factory=list)


class StartupValidationError(Exception):
    def __init__(self, report: StartupReport):
        self.report = report
        failed = [c.name for c in report.checks if not c.passed]
        super().__init__(f"Startup validation failed: {failed}")


def _check_settings() -> StartupCheck:
    try:
        settings = get_settings()
        required = ["database_url", "jwt_secret", "nats_url"]
        missing = [r for r in required if not getattr(settings, r, None)]
        if missing:
            return StartupCheck(name="settings", passed=False, detail=f"missing: {missing}")
        return StartupCheck(name="settings", passed=True)
    except Exception as e:
        return StartupCheck(name="settings", passed=False, detail=str(e))


def _check_ports() -> StartupCheck:
    try:
        settings = get_settings()
        ports = {
            "manager (app_port)": getattr(settings, "app_port", None),
            "devops": getattr(settings, "devops_service_port", None),
            "monitoring": settings.monitoring_service_port,
            "incident_response": settings.incident_response_service_port,
        }
        missing = [k for k, v in ports.items() if not v]
        collisions = [v for v in ports.values() if v and list(ports.values()).count(v) > 1]
        if missing:
            return StartupCheck(name="ports", passed=False, detail=f"unset: {missing}")
        if collisions:
            return StartupCheck(name="ports", passed=False, detail=f"colliding port(s): {sorted(set(collisions))}")
        return StartupCheck(name="ports", passed=True, detail=str(ports))
    except Exception as e:
        return StartupCheck(name="ports", passed=False, detail=str(e))


def _check_agent_registry() -> StartupCheck:
    from services.integration.orchestrator import validate_agent_registry
    r = validate_agent_registry()
    return StartupCheck(name="agent_registry", passed=r.passed,
                         detail=f"{r.total_agents} agents" if r.passed else str([f.detail for f in r.findings]))


def _check_workflow_registration() -> StartupCheck:
    from services.integration.lifecycle import validate_all_workflows
    results = validate_all_workflows()
    failing = [name for name, r in results.items() if not r.built]
    if failing:
        return StartupCheck(name="workflow_registration", passed=False, detail=f"failed to build: {failing}")
    return StartupCheck(name="workflow_registration", passed=True, detail=f"{len(results)} graphs built")


def _check_workflow_diagnostics() -> StartupCheck:
    """M3.10 §4 Startup Validation: runs the richer workflow_validator
    over every registered LangGraph and logs a per-workflow ✓/WARNING
    line. Diagnostics issues (unreachable nodes, missing END, missing
    START, invalid route targets) never fail startup — this check's
    `passed` reflects whether the diagnostics *ran*, not whether every
    workflow is perfectly clean; a workflow with warnings still counts
    as a successful check here (see StartupReport.checks vs. the
    granular per-workflow detail logged alongside it)."""
    from services.integration.validators.workflow_validator import validate_all_workflows_detailed

    try:
        reports = validate_all_workflows_detailed()
    except Exception as e:
        return StartupCheck(name="workflow_diagnostics", passed=False, detail=f"diagnostics crashed: {e}")

    for name, report in reports.items():
        if report.healthy and not report.warnings:
            log.info("workflow_diagnostics_check", workflow=name, status="ok")
        else:
            issues = report.errors + report.warnings
            log.warning("workflow_diagnostics_check", workflow=name, status="warning", issues=issues)

    unhealthy = [name for name, r in reports.items() if not r.healthy]
    detail = f"{len(reports)} workflows checked"
    if unhealthy:
        detail += f"; unhealthy: {unhealthy}"
    # Always passes (diagnostics ran successfully) — startup must
    # continue regardless of individual workflow health per spec §4.
    return StartupCheck(name="workflow_diagnostics", passed=True, detail=detail)


async def _check_migrations(db_factory: Any) -> StartupCheck:
    if db_factory is None:
        return StartupCheck(name="db_migrations", passed=False, detail="no db_factory configured")
    try:
        from sqlalchemy import text
        async with db_factory() as db:
            result = await db.execute(text("SELECT version_num FROM alembic_version"))
            version = result.scalar_one_or_none()
        if version is None:
            return StartupCheck(name="db_migrations", passed=False, detail="no alembic_version row")
        return StartupCheck(name="db_migrations", passed=True, detail=f"head={version}")
    except Exception as e:
        return StartupCheck(name="db_migrations", passed=False, detail=str(e))


async def _check_repository_connectivity(db_factory: Any) -> StartupCheck:
    if db_factory is None:
        return StartupCheck(name="repository_connectivity", passed=False, detail="no db_factory configured")
    try:
        from sqlalchemy import select
        from infrastructure.database.models import Project
        async with db_factory() as db:
            await db.execute(select(Project).limit(1))
        return StartupCheck(name="repository_connectivity", passed=True)
    except Exception as e:
        return StartupCheck(name="repository_connectivity", passed=False, detail=str(e))


def _check_nats_subjects() -> StartupCheck:
    from services.integration.event_router import EVENT_MANIFEST
    if not EVENT_MANIFEST:
        return StartupCheck(name="nats_subjects", passed=False, detail="empty manifest")
    return StartupCheck(name="nats_subjects", passed=True, detail=f"{len(EVENT_MANIFEST)} known subjects")


def _check_artifact_registry() -> StartupCheck:
    try:
        from infrastructure.database.repositories import ArtifactRepository  # noqa: F401
        return StartupCheck(name="artifact_registry", passed=True)
    except Exception as e:
        return StartupCheck(name="artifact_registry", passed=False, detail=str(e))


async def run_startup_checks(
    db_factory: Any = None, strict: bool = True,
) -> StartupReport:
    checks: List[StartupCheck] = [
        _check_settings(),
        _check_ports(),
        _check_agent_registry(),
        _check_workflow_registration(),
        _check_workflow_diagnostics(),
        _check_nats_subjects(),
        _check_artifact_registry(),
        await _check_migrations(db_factory),
        await _check_repository_connectivity(db_factory),
    ]
    report = StartupReport(passed=all(c.passed for c in checks), checks=checks)
    if strict and not report.passed:
        raise StartupValidationError(report)
    return report
