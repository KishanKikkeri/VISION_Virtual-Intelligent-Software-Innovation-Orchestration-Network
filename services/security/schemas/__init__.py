"""
services/security/schemas — API & event contracts for M3.5.
================================================================
Nothing outside this module should define ad-hoc request/response
dict shapes for the Security Service's HTTP API or NATS events.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from services.security.models import RiskAssessment, SecurityFinding, SecurityReport, SecurityTaskStatus


# ── Requests ──────────────────────────────────────────────────

class StartSecurityRequest(BaseModel):
    project_id:   str
    workflow_id:  str
    feature_name: str = "default"


class RetryDecisionRequest(BaseModel):
    project_id:  str
    approved:    bool
    approved_by: Optional[str] = None
    feedback:    Optional[str] = None


# ── Responses ─────────────────────────────────────────────────

class SecurityTaskResponse(BaseModel):
    task_id:         str
    team:            str
    worker_agent_id: str
    status:          SecurityTaskStatus
    retry_count:     int
    depends_on:      List[str]
    failure_reason:  Optional[str] = None


class SecurityPlanResponse(BaseModel):
    plan_id:      str
    project_id:   str
    feature_name: str
    total_tasks:  int
    tasks:        List[SecurityTaskResponse]


class SecurityStatusResponse(BaseModel):
    project_id:      str
    phase_status:    str
    plan:            Optional[SecurityPlanResponse] = None
    scans_completed: int = 0
    risk_score:      float = 0.0
    security_report: Optional[SecurityReport] = None
    risk_assessment: Optional[RiskAssessment] = None
    findings:        List[SecurityFinding] = Field(default_factory=list)
    updated_at:      datetime = Field(default_factory=datetime.utcnow)


class SecurityCompletedEvent(BaseModel):
    project_id:   str
    workflow_id:  str
    feature_name: str
    passed:       bool
    risk_level:   str
    risk_score:   float
    finding_count: int


# ── Errors ────────────────────────────────────────────────────

class SecurityServiceError(Exception):
    """Base class for all Security Service errors."""


class NoEngineeringArtifactsError(SecurityServiceError):
    """Raised when Security is invoked without approved Engineering artifacts."""


class SecurityGateBlockedError(SecurityServiceError):
    """Raised when the Security gate blocks and no more retry budget remains."""


class DeadLetterError(SecurityServiceError):
    """Raised when a Security task exhausts its retry budget and is dead-lettered."""
