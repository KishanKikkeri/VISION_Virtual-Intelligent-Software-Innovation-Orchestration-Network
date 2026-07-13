"""
services/monitoring/models.py — M3.7 Monitoring Service core models.
================================================================
Flat module (not a package), following the M3.6 DevOps precedent — the
M3.7 spec's explicit deliverables layout lists `services/monitoring/
models.py` as a single file (see
docs/M3.7_Monitoring_Service_Specification_v1.md §9 / Deliverables).

These Pydantic models are the in-memory/API shapes used across
providers, workers, leads, and head; `services/monitoring/integration/
monitoring_repository.py` maps between them and the ORM rows in
`infrastructure/database/models.py` (Metric, MetricSample, SystemHealth,
Alert, AlertHistory, Dashboard, DashboardWidget, MonitoringLog,
MonitoringTrace, CapacityForecast).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# -- Enums --------------------------------------------------------

class HealthStatus(str, Enum):
    HEALTHY  = "healthy"
    WARNING  = "warning"
    CRITICAL = "critical"


class AlertSeverity(str, Enum):
    INFO     = "info"
    WARNING  = "warning"
    CRITICAL = "critical"


class AlertStatus(str, Enum):
    OPEN         = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED     = "resolved"


class MonitoringTaskStatus(str, Enum):
    PENDING       = "pending"
    RUNNING       = "running"
    COMPLETED     = "completed"
    FAILED        = "failed"
    ESCALATED     = "escalated"
    DEAD_LETTERED = "dead_lettered"


# Frozen per spec §5 — the 9 systems Monitoring collects from.
class MonitoredComponent(str, Enum):
    POSTGRES       = "postgres"
    NATS           = "nats"
    QDRANT         = "qdrant"
    DOCKER         = "docker"
    WEBSOCKET      = "websocket"
    LLM_PROVIDERS  = "llm_providers"
    AGENT_RUNTIME  = "agent_runtime"
    REPOSITORY     = "repository"
    DEPLOYMENTS    = "deployments"


# Frozen weights per spec §0 Decision 3 — deterministic, reproducible in tests.
COMPONENT_WEIGHTS: Dict[MonitoredComponent, float] = {
    MonitoredComponent.POSTGRES:      3.0,
    MonitoredComponent.NATS:          3.0,
    MonitoredComponent.QDRANT:        2.0,
    MonitoredComponent.DOCKER:        2.0,
    MonitoredComponent.WEBSOCKET:     1.0,
    MonitoredComponent.LLM_PROVIDERS: 3.0,
    MonitoredComponent.AGENT_RUNTIME: 3.0,
    MonitoredComponent.REPOSITORY:    2.0,
    MonitoredComponent.DEPLOYMENTS:   2.0,
}

# Frozen thresholds per spec §0 Decision 3.
HEALTHY_THRESHOLD  = 90.0
WARNING_THRESHOLD  = 70.0


def status_for_score(score: float) -> HealthStatus:
    """Deterministic threshold classification — see spec §0 Decision 3."""
    if score >= HEALTHY_THRESHOLD:
        return HealthStatus.HEALTHY
    if score >= WARNING_THRESHOLD:
        return HealthStatus.WARNING
    return HealthStatus.CRITICAL


# -- Metric collection (Metrics Lead) --------------------------------

class MetricSample(BaseModel):
    """One collected data point. Mirrors infrastructure/database/models.py's MetricSample table."""
    name:       str
    component:  MonitoredComponent
    value:      float
    unit:       Optional[str] = None
    labels:     Dict[str, str] = Field(default_factory=dict)
    project_id: Optional[str] = None
    sampled_at: datetime = Field(default_factory=datetime.utcnow)


class MetricsSnapshot(BaseModel):
    """Artifact content for `metrics_snapshot` (see spec §4)."""
    samples:      List[MetricSample] = Field(default_factory=list)
    collected_at: datetime = Field(default_factory=datetime.utcnow)


# -- Health scoring (Monitoring Head) ---------------------------------

class ComponentScore(BaseModel):
    component: MonitoredComponent
    score:     float = Field(ge=0.0, le=100.0)
    weight:    float
    reason:    str = ""


class SystemHealthReport(BaseModel):
    """Artifact content for `system_health_report` (see spec §4)."""
    health_score:      float = Field(ge=0.0, le=100.0)
    status:            HealthStatus
    component_scores:  Dict[str, float] = Field(default_factory=dict)
    computed_at:       datetime = Field(default_factory=datetime.utcnow)


# -- Dashboards (Dashboard Worker) --------------------------------------

class DashboardWidget(BaseModel):
    widget_id:   str = Field(default_factory=lambda: str(uuid.uuid4()))
    widget_type: str            # e.g. "line_chart", "gauge", "table"
    title:       str
    config:      Dict[str, Any] = Field(default_factory=dict)
    position:    int = 0


class DashboardConfiguration(BaseModel):
    """Artifact content for `dashboard_configuration` (see spec §4)."""
    name:    str = "platform_overview"
    widgets: List[DashboardWidget] = Field(default_factory=list)
    layout:  Dict[str, Any] = Field(default_factory=dict)


# -- Alerts (Alert Worker) ------------------------------------------------

class AlertRule(BaseModel):
    component:            MonitoredComponent
    severity:             AlertSeverity
    threshold_score:      float
    dedup_window_seconds: int = 300


class AlertConfiguration(BaseModel):
    """Artifact content for `alert_configuration` (see spec §4)."""
    rules: List[AlertRule] = Field(default_factory=list)


class AlertEvent(BaseModel):
    alert_id:   str = Field(default_factory=lambda: str(uuid.uuid4()))
    component:  MonitoredComponent
    severity:   AlertSeverity
    message:    str
    raised_at:  datetime = Field(default_factory=datetime.utcnow)


# -- Performance / observability (Observability Lead) ------------------

class PerformanceReport(BaseModel):
    """Artifact content for `performance_report` (see spec §4)."""
    p95_latency_ms:  float = 0.0
    error_rate:      float = 0.0
    trace_hotspots:  List[str] = Field(default_factory=list)
    computed_at:     datetime = Field(default_factory=datetime.utcnow)


# -- Incident candidates (Alerting Lead, handoff to M3.8) --------------

class IncidentCandidate(BaseModel):
    """Artifact content for `incident_candidate` (see spec §0 Decision 5 / §4)."""
    incident_id:    str = Field(default_factory=lambda: str(uuid.uuid4()))
    component:      MonitoredComponent
    severity:       AlertSeverity
    breach_cycles:  int
    evidence_refs:  List[str] = Field(default_factory=list)
    created_at:     datetime = Field(default_factory=datetime.utcnow)


# -- Capacity forecasting (Metrics Lead) ------------------------------

class CapacityForecast(BaseModel):
    """Artifact content contribution for `capacity_forecast` (see spec §4)."""
    component:           MonitoredComponent
    trend_slope:         float = 0.0
    projected_breach_at: Optional[datetime] = None
    computed_at:         datetime = Field(default_factory=datetime.utcnow)


# -- Monitoring cycle task bookkeeping (mirrors DevOpsTask/DevOpsPlan) --

class MonitoringTask(BaseModel):
    task_id:         str = Field(default_factory=lambda: str(uuid.uuid4()))
    worker_agent_id: str
    description:     str = ""
    depends_on:       List[str] = Field(default_factory=list)
    status:           MonitoringTaskStatus = MonitoringTaskStatus.PENDING
    retry_count:      int = 0
    max_retries:      int = 3
    failure_reason:   Optional[str] = None

    def can_run(self, completed_task_ids: set) -> bool:
        return (
            self.status == MonitoringTaskStatus.PENDING
            and all(dep in completed_task_ids for dep in self.depends_on)
        )
