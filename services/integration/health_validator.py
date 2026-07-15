"""
services/integration/health_validator.py
=================================
Spec §4 Global Health Check + §9 Repository Validation.

Every check degrades gracefully — a component that can't be reached
is FAILED, not an unhandled exception, since this module's entire
purpose is to be trustworthy when the rest of the platform is
unhealthy.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

COMPONENTS = [
    "database", "nats", "repository", "manager", "product", "architecture",
    "engineering", "qa", "security", "devops", "monitoring", "incident_response",
    "websocket", "telemetry",
]

# Every table this milestone knows the platform should have, grouped by
# the department that owns it (spec §9). Built from infrastructure/
# database/models.py, verified table-by-table during reconnaissance.
EXPECTED_TABLES: Dict[str, List[str]] = {
    "core":               ["users", "projects", "workflow_phases", "artifacts",
                            "audit_events", "token_ledger"],
    "repository":         ["repositories", "branches", "pull_requests", "repository_events"],
    "devops":             ["deployments", "deployment_history", "deployment_health",
                            "release_metadata", "rollback_records"],
    "monitoring":         ["metrics", "metric_samples", "system_health", "alerts",
                            "alert_history", "dashboards", "dashboard_widgets",
                            "logs", "traces", "capacity_forecast"],
    "incident_response":  ["incidents", "incident_timeline_events", "incident_evidence",
                            "recovery_actions", "incident_reports"],
    "integration":        ["platform_reports", "validation_results", "dependency_checks"],
}


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"


class ComponentHealth(BaseModel):
    name: str
    status: HealthStatus
    detail: str = ""


class PlatformHealthReport(BaseModel):
    overall: HealthStatus
    components: List[ComponentHealth] = Field(default_factory=list)


def _agent_id_for_department(department: str) -> Optional[str]:
    return {
        "manager": "manager_agent", "product": "product_head", "architecture": "architecture_head",
        "engineering": "engineering_head", "qa": "qa_head", "security": "security_head",
        "devops": "devops_head", "monitoring": "monitoring_head",
        "incident_response": "incident_response_head",
    }.get(department)


async def check_database(db_factory: Any) -> ComponentHealth:
    if db_factory is None:
        return ComponentHealth(name="database", status=HealthStatus.FAILED, detail="no db_factory configured")
    try:
        from sqlalchemy import text
        async with db_factory() as db:
            await db.execute(text("SELECT 1"))
        return ComponentHealth(name="database", status=HealthStatus.HEALTHY)
    except Exception as e:
        return ComponentHealth(name="database", status=HealthStatus.FAILED, detail=str(e))


async def check_nats(nats: Any) -> ComponentHealth:
    if nats is None:
        return ComponentHealth(name="nats", status=HealthStatus.FAILED, detail="not connected")
    try:
        is_connected = getattr(nats, "is_connected", None)
        if callable(is_connected):
            ok = is_connected()
        elif is_connected is not None:
            ok = bool(is_connected)
        else:
            ok = True  # best-effort — client object exists, assume healthy
        return ComponentHealth(name="nats", status=HealthStatus.HEALTHY if ok else HealthStatus.DEGRADED)
    except Exception as e:
        return ComponentHealth(name="nats", status=HealthStatus.FAILED, detail=str(e))


def check_department_agent(department: str, factory: Any) -> ComponentHealth:
    """HEALTHY if the department's head agent instantiates via
    AgentFactory; this is a structural smoke check, not a live task run."""
    agent_id = _agent_id_for_department(department)
    if agent_id is None:
        return ComponentHealth(name=department, status=HealthStatus.FAILED, detail="unknown department")
    if factory is None:
        return ComponentHealth(name=department, status=HealthStatus.FAILED, detail="no AgentFactory configured")
    try:
        agent = factory.create(agent_id)
        if agent.department != department:
            return ComponentHealth(name=department, status=HealthStatus.DEGRADED,
                                    detail=f"agent department mismatch: {agent.department!r} != {department!r}")
        return ComponentHealth(name=department, status=HealthStatus.HEALTHY)
    except Exception as e:
        return ComponentHealth(name=department, status=HealthStatus.FAILED, detail=str(e))


def check_websocket() -> ComponentHealth:
    try:
        from infrastructure.websocket.manager import ws_manager  # noqa: F401
        return ComponentHealth(name="websocket", status=HealthStatus.HEALTHY)
    except Exception as e:
        return ComponentHealth(name="websocket", status=HealthStatus.FAILED, detail=str(e))


def check_telemetry() -> ComponentHealth:
    try:
        from infrastructure.monitoring.telemetry import configure_telemetry  # noqa: F401
        return ComponentHealth(name="telemetry", status=HealthStatus.HEALTHY)
    except Exception as e:
        return ComponentHealth(name="telemetry", status=HealthStatus.FAILED, detail=str(e))


def check_repository_module() -> ComponentHealth:
    try:
        from services.repository.managers import RepositoryDeps  # noqa: F401
        return ComponentHealth(name="repository", status=HealthStatus.HEALTHY)
    except Exception as e:
        return ComponentHealth(name="repository", status=HealthStatus.FAILED, detail=str(e))


def overall_status(components: List[ComponentHealth]) -> HealthStatus:
    """Deterministic: any FAILED -> FAILED overall unless the platform
    still has quorum (>= 80% healthy), any DEGRADED (with no FAILED)
    -> DEGRADED, else HEALTHY."""
    if not components:
        return HealthStatus.FAILED
    failed = [c for c in components if c.status == HealthStatus.FAILED]
    degraded = [c for c in components if c.status == HealthStatus.DEGRADED]
    healthy_ratio = sum(1 for c in components if c.status == HealthStatus.HEALTHY) / len(components)
    if failed and healthy_ratio < 0.8:
        return HealthStatus.FAILED
    if failed or degraded:
        return HealthStatus.DEGRADED
    return HealthStatus.HEALTHY


async def generate_health_report(db_factory: Any = None, nats: Any = None, factory: Any = None) -> PlatformHealthReport:
    components: List[ComponentHealth] = [
        await check_database(db_factory),
        await check_nats(nats),
        check_repository_module(),
    ]
    for dept in ("manager", "product", "architecture", "engineering", "qa",
                 "security", "devops", "monitoring", "incident_response"):
        components.append(check_department_agent(dept, factory))
    components.append(check_websocket())
    components.append(check_telemetry())
    return PlatformHealthReport(overall=overall_status(components), components=components)


def validate_repository_layer(existing_tables: List[str]) -> Dict[str, List[str]]:
    """Spec §9 Repository Validation — given a live list of table names
    (e.g. from `inspect(bind).get_table_names()`), returns
    {group: [missing tables]} for every group in EXPECTED_TABLES
    (empty dict overall = fully migrated)."""
    existing = set(existing_tables)
    missing: Dict[str, List[str]] = {}
    for group, tables in EXPECTED_TABLES.items():
        gap = sorted(set(tables) - existing)
        if gap:
            missing[group] = gap
    return missing
