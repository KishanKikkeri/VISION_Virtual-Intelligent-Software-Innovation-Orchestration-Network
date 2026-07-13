"""
services/monitoring/integration/monitoring_repository.py
=========================================================
Repository-pattern wrapper around the M3.7 ORM tables (Metric,
MetricSample, SystemHealth, Alert, AlertHistory, Dashboard,
DashboardWidget, MonitoringLog, MonitoringTrace, CapacityForecast —
see infrastructure/database/models.py). Kept inside services/monitoring/
rather than the shared infrastructure/database/repositories/__init__.py,
mirroring DevOps's self-contained `deployment_repository.py` and
Security's `repository_client.py` precedent.

All methods are static and take an AsyncSession as their first
argument, matching the existing ArtifactRepository convention.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select, update

from infrastructure.database.models import (
    Alert, AlertHistory, CapacityForecast, Dashboard, DashboardWidget,
    Metric, MetricSample, MonitoringLog, MonitoringTrace, SystemHealth,
)


class MetricRepository:
    @staticmethod
    async def get_or_create(db, name: str, component: str, unit: Optional[str] = None) -> Metric:
        r = await db.execute(select(Metric).where(Metric.name == name, Metric.component == component))
        existing = r.scalar_one_or_none()
        if existing:
            return existing
        m = Metric(name=name, component=component, unit=unit)
        db.add(m)
        await db.flush()
        return m


class MetricSampleRepository:
    @staticmethod
    async def record(db, metric_id: str, value: float, labels: Optional[Dict[str, Any]] = None,
                      project_id: Optional[str] = None) -> MetricSample:
        row = MetricSample(metric_id=metric_id, value=value, labels=labels or {}, project_id=project_id)
        db.add(row)
        await db.flush()
        return row

    @staticmethod
    async def trailing_values(db, metric_id: str, limit: int = 20) -> List[float]:
        r = await db.execute(
            select(MetricSample).where(MetricSample.metric_id == metric_id)
            .order_by(MetricSample.sampled_at.desc()).limit(limit))
        rows = list(r.scalars().all())
        return [float(row.value) for row in reversed(rows)]


class SystemHealthRepository:
    @staticmethod
    async def record(db, health_score: float, status: str,
                      component_scores: Dict[str, Any]) -> SystemHealth:
        row = SystemHealth(health_score=health_score, status=status, component_scores=component_scores)
        db.add(row)
        await db.flush()
        return row

    @staticmethod
    async def latest(db) -> Optional[SystemHealth]:
        r = await db.execute(select(SystemHealth).order_by(SystemHealth.cycle_at.desc()).limit(1))
        return r.scalar_one_or_none()


class AlertRepository:
    @staticmethod
    async def open_alert(db, component: str, severity: str, message: str,
                          project_id: Optional[str] = None) -> Alert:
        row = Alert(component=component, severity=severity, message=message,
                     status="open", project_id=project_id)
        db.add(row)
        await db.flush()
        return row

    @staticmethod
    async def find_open(db, component: str, severity: str) -> Optional[Alert]:
        r = await db.execute(
            select(Alert).where(Alert.component == component, Alert.severity == severity,
                                 Alert.status == "open"))
        return r.scalar_one_or_none()

    @staticmethod
    async def acknowledge(db, alert_id: str) -> None:
        await db.execute(update(Alert).where(Alert.id == alert_id)
                          .values(status="acknowledged", last_seen_at=datetime.utcnow()))

    @staticmethod
    async def list_open(db) -> List[Alert]:
        r = await db.execute(select(Alert).where(Alert.status != "resolved")
                              .order_by(Alert.last_seen_at.desc()))
        return list(r.scalars().all())


class AlertHistoryRepository:
    @staticmethod
    async def record(db, alert_id: str, action: str, actor: str = "alert_worker") -> AlertHistory:
        row = AlertHistory(alert_id=alert_id, action=action, actor=actor)
        db.add(row)
        await db.flush()
        return row


class DashboardRepository:
    @staticmethod
    async def upsert(db, name: str, layout: Dict[str, Any]) -> Dashboard:
        r = await db.execute(select(Dashboard).where(Dashboard.name == name))
        existing = r.scalar_one_or_none()
        if existing:
            existing.layout = layout
            existing.updated_at = datetime.utcnow()
            await db.flush()
            return existing
        row = Dashboard(name=name, layout=layout)
        db.add(row)
        await db.flush()
        return row

    @staticmethod
    async def get_by_name(db, name: str) -> Optional[Dashboard]:
        r = await db.execute(select(Dashboard).where(Dashboard.name == name))
        return r.scalar_one_or_none()


class DashboardWidgetRepository:
    @staticmethod
    async def replace_all(db, dashboard_id: str, widgets: List[Dict[str, Any]]) -> List[DashboardWidget]:
        from sqlalchemy import delete
        await db.execute(delete(DashboardWidget).where(DashboardWidget.dashboard_id == dashboard_id))
        rows = []
        for w in widgets:
            row = DashboardWidget(dashboard_id=dashboard_id, widget_type=w["widget_type"],
                                   config=w.get("config", {}), position=w.get("position", 0))
            db.add(row)
            rows.append(row)
        await db.flush()
        return rows


class CapacityForecastRepository:
    @staticmethod
    async def record(db, component: str, trend_slope: float,
                      projected_breach_at: Optional[datetime] = None) -> CapacityForecast:
        row = CapacityForecast(component=component, trend_slope=trend_slope,
                                projected_breach_at=projected_breach_at)
        db.add(row)
        await db.flush()
        return row


class MonitoringLogRepository:
    @staticmethod
    async def record(db, service: str, level: str, message: str,
                      context: Optional[Dict[str, Any]] = None) -> MonitoringLog:
        row = MonitoringLog(service=service, level=level, message=message, context=context or {})
        db.add(row)
        await db.flush()
        return row


class MonitoringTraceRepository:
    @staticmethod
    async def record(db, trace_id: str, span_id: str, service: str,
                      duration_ms: float, status: str = "ok") -> MonitoringTrace:
        row = MonitoringTrace(trace_id=trace_id, span_id=span_id, service=service,
                               duration_ms=duration_ms, status=status)
        db.add(row)
        await db.flush()
        return row
