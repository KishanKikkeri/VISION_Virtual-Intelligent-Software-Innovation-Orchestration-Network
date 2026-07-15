"""
services/incident_response/schemas.py — API & event contracts for M3.8.
================================================================
Nothing outside this module should define ad-hoc request/response dict
shapes for the Incident Response Service's HTTP API or NATS events.
Mirrors services/monitoring/schemas.py.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from services.incident_response.models import (
    IncidentStatus,
    RecoveryActionStatus,
    RecoveryActionType,
)
from services.monitoring.models import AlertSeverity, MonitoredComponent


# -- Requests -----------------------------------------------------

class ManualIncidentRequest(BaseModel):
    """Operator escape hatch — normally incidents are created automatically
    from the `monitoring.incident` NATS event (see api/events.py)."""
    component:     MonitoredComponent
    severity:      AlertSeverity
    breach_cycles: int = 1
    reason:        str = "manually_opened"


class CloseIncidentRequest(BaseModel):
    incident_id: str
    closed_by:   Optional[str] = None


# -- Responses ------------------------------------------------------

class IncidentResponse(BaseModel):
    incident_id:   str
    component:     str
    severity:      AlertSeverity
    status:        IncidentStatus
    breach_cycles: int
    created_at:    datetime
    updated_at:    datetime
    resolved_at:   Optional[datetime] = None
    closed_at:     Optional[datetime] = None


class RecoveryActionResponse(BaseModel):
    incident_id:  str
    action_type:  RecoveryActionType
    status:       RecoveryActionStatus
    triggered_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    detail:       Dict[str, Any] = Field(default_factory=dict)


class IncidentTimelineResponse(BaseModel):
    incident_id: str
    entries:     List[Dict[str, Any]] = Field(default_factory=list)


class IncidentPhaseCompletedEvent(BaseModel):
    incident_id: str
    status:      str
    component:   str


# -- Errors ---------------------------------------------------------

class IncidentResponseServiceError(Exception):
    """Base class for all Incident Response Service errors."""


class IncidentNotFoundError(IncidentResponseServiceError):
    """Raised when a request references an unknown incident_id."""


class ProviderUnavailableError(IncidentResponseServiceError):
    """Raised (internally, best-effort caught) when a provider cannot be constructed at all."""
