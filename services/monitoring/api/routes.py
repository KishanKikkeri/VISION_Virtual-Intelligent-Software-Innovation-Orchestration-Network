"""
services/monitoring/api/routes.py
=================================
Monitoring Service's HTTP surface, per spec §10:
    GET /metrics    GET /health     GET /dashboard   GET /alerts
    POST /alerts/ack  GET /performance  GET /services  GET /agents
    GET /deployments  GET /costs

Reads are DB-backed (system_health/alerts/dashboards/etc, spec §0
Decision 4) rather than served from an in-memory cycle registry, since
Monitoring's whole point is durable historical state, not just "latest
in-process result" the way DevOps's `_STATUS` registry is.
"""
from __future__ import annotations

from typing import Any, Dict, List

import structlog
from fastapi import APIRouter, Depends, HTTPException

from infrastructure.database.connection import get_db
from services.monitoring.integration.monitoring_repository import (
    AlertHistoryRepository, AlertRepository, DashboardRepository, MetricSampleRepository,
    SystemHealthRepository,
)
from services.monitoring.models import MonitoredComponent, status_for_score
from services.monitoring.schemas import (
    AcknowledgeAlertRequest, AlertResponse, ComponentDetailResponse, HealthResponse,
)

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/monitoring", tags=["Monitoring"])


@router.get("/metrics")
async def get_metrics(component: str = None, db=Depends(get_db)) -> Dict[str, Any]:
    """Latest metric_samples, optionally filtered by component (spec §10)."""
    from sqlalchemy import select
    from infrastructure.database.models import Metric, MetricSample as MetricSampleRow

    query = select(MetricSampleRow).order_by(MetricSampleRow.sampled_at.desc()).limit(100)
    result = await db.execute(query)
    rows = list(result.scalars().all())
    if component:
        metric_ids = {mid async for mid in _metric_ids_for_component(db, component)}
        rows = [r for r in rows if r.metric_id in metric_ids]
    return {"samples": [
        {"metric_id": r.metric_id, "value": float(r.value), "labels": r.labels,
         "sampled_at": r.sampled_at.isoformat()}
        for r in rows
    ]}


async def _metric_ids_for_component(db, component: str):
    from sqlalchemy import select
    from infrastructure.database.models import Metric
    result = await db.execute(select(Metric).where(Metric.component == component))
    for m in result.scalars().all():
        yield m.id


@router.get("/health", response_model=HealthResponse)
async def get_health(db=Depends(get_db)) -> HealthResponse:
    latest = await SystemHealthRepository.latest(db)
    if latest is None:
        return HealthResponse(health_score=0.0, status="critical", component_scores={})
    return HealthResponse(
        health_score=float(latest.health_score), status=latest.status,
        component_scores=latest.component_scores, cycle_at=latest.cycle_at,
    )


@router.get("/dashboard")
async def get_dashboard(name: str = "platform_overview", db=Depends(get_db)) -> Dict[str, Any]:
    dashboard = await DashboardRepository.get_by_name(db, name)
    if dashboard is None:
        raise HTTPException(404, f"No dashboard named {name!r} yet — wait for the first cycle to complete")
    return {"name": dashboard.name, "layout": dashboard.layout, "updated_at": dashboard.updated_at.isoformat()}


@router.get("/alerts", response_model=List[AlertResponse])
async def get_alerts(db=Depends(get_db)) -> List[AlertResponse]:
    rows = await AlertRepository.list_open(db)
    return [
        AlertResponse(alert_id=r.id, component=r.component, severity=r.severity, message=r.message,
                     status=r.status, first_seen_at=r.first_seen_at, last_seen_at=r.last_seen_at)
        for r in rows
    ]


@router.post("/alerts/ack")
async def acknowledge_alert(req: AcknowledgeAlertRequest, db=Depends(get_db)) -> Dict[str, Any]:
    await AlertRepository.acknowledge(db, req.alert_id)
    await AlertHistoryRepository.record(db, req.alert_id, "acknowledged",
                                        actor=req.acknowledged_by or "operator")
    return {"status": "acknowledged", "alert_id": req.alert_id}


@router.get("/performance")
async def get_performance(db=Depends(get_db)) -> Dict[str, Any]:
    from sqlalchemy import select
    from infrastructure.database.models import Artifact

    result = await db.execute(
        select(Artifact).where(Artifact.artifact_type == "performance_report")
        .order_by(Artifact.created_at.desc()).limit(1))
    latest = result.scalar_one_or_none()
    if latest is None:
        return {"p95_latency_ms": 0.0, "error_rate": 0.0, "trace_hotspots": []}
    return latest.content or {}


async def _component_detail(db, component: MonitoredComponent) -> ComponentDetailResponse:
    latest = await SystemHealthRepository.latest(db)
    score = 0.0
    if latest and component.value in (latest.component_scores or {}):
        score = float(latest.component_scores[component.value])
    return ComponentDetailResponse(component=component.value, score=score, status=status_for_score(score))


@router.get("/services")
async def get_services(db=Depends(get_db)) -> Dict[str, Any]:
    """Per-service component_score breakdown (spec §10)."""
    latest = await SystemHealthRepository.latest(db)
    return {"component_scores": (latest.component_scores if latest else {}),
            "status": (latest.status if latest else "critical")}


@router.get("/agents")
async def get_agents(db=Depends(get_db)) -> Dict[str, Any]:
    """agent_runtime component detail, from telemetry_provider (spec §10)."""
    detail = await _component_detail(db, MonitoredComponent.AGENT_RUNTIME)
    return detail.model_dump()


@router.get("/deployments")
async def get_deployments_health(db=Depends(get_db)) -> Dict[str, Any]:
    """deployments component detail — read-only passthrough (spec §10)."""
    detail = await _component_detail(db, MonitoredComponent.DEPLOYMENTS)
    return detail.model_dump()


@router.get("/costs")
async def get_costs(db=Depends(get_db)) -> Dict[str, Any]:
    """llm_providers component detail — token/cost (spec §10)."""
    detail = await _component_detail(db, MonitoredComponent.LLM_PROVIDERS)
    return detail.model_dump()
