"""
services/monitoring/schemas.py — API & event contracts for M3.7.
================================================================
Nothing outside this module should define ad-hoc request/response
dict shapes for the Monitoring Service's HTTP API or NATS events.
Mirrors services/devops/schemas.py.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from services.monitoring.models import (
    AlertSeverity,
    AlertStatus,
    DashboardConfiguration,
    HealthStatus,
    PerformanceReport,
)


# -- Requests -----------------------------------------------------

class AcknowledgeAlertRequest(BaseModel):
    alert_id:    str
    acknowledged_by: Optional[str] = None


# -- Responses ------------------------------------------------------

class HealthResponse(BaseModel):
    health_score:      float
    status:            HealthStatus
    component_scores:  Dict[str, float] = Field(default_factory=dict)
    cycle_at:          Optional[datetime] = None


class MetricSampleResponse(BaseModel):
    name:       str
    component:  str
    value:      float
    unit:       Optional[str] = None
    labels:     Dict[str, Any] = Field(default_factory=dict)
    sampled_at: datetime


class AlertResponse(BaseModel):
    alert_id:      str
    component:     str
    severity:      AlertSeverity
    message:       str
    status:        AlertStatus
    first_seen_at: datetime
    last_seen_at:  datetime


class ComponentDetailResponse(BaseModel):
    component: str
    score:     float
    status:    HealthStatus
    detail:    Dict[str, Any] = Field(default_factory=dict)


class MonitoringPhaseCompletedEvent(BaseModel):
    health_score: float
    status:       str
    cycle_count:  int


# -- Errors ---------------------------------------------------------

class MonitoringServiceError(Exception):
    """Base class for all Monitoring Service errors."""


class AlertNotFoundError(MonitoringServiceError):
    """Raised when POST /alerts/ack references an unknown alert_id."""


class ProviderUnavailableError(MonitoringServiceError):
    """Raised (internally, best-effort caught) when a provider cannot be constructed at all."""
