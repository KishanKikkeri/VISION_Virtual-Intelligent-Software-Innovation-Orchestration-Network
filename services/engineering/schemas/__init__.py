"""
services/engineering/schemas — API & event contracts for M3.3.
==================================================================
Nothing outside this module should define ad-hoc request/response
dict shapes for the Engineering Service's HTTP API or NATS events.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from services.engineering.models import (
    BuildResult,
    EngineeringTaskStatus,
    ImplementationPlan,
)


# ── Requests ──────────────────────────────────────────────────

class StartEngineeringRequest(BaseModel):
    project_id:   str
    workflow_id:  str
    feature_name: str = "default"


class ApprovalDecisionRequest(BaseModel):
    project_id:  str
    approved:    bool
    approved_by: Optional[str] = None
    feedback:    Optional[str] = None


# ── Responses ─────────────────────────────────────────────────

class EngineeringTaskResponse(BaseModel):
    task_id:         str
    team:            str
    worker_agent_id: str
    status:          EngineeringTaskStatus
    retry_count:     int
    depends_on:      List[str]
    failure_reason:  Optional[str] = None


class ImplementationPlanResponse(BaseModel):
    plan_id:      str
    project_id:   str
    feature_name: str
    total_tasks:  int
    tasks:        List[EngineeringTaskResponse]


class EngineeringStatusResponse(BaseModel):
    project_id:        str
    phase_status:      str
    plan:              Optional[ImplementationPlanResponse] = None
    modules_generated: int = 0
    review_cycles_run: int = 0
    build_result:      Optional[BuildResult] = None
    updated_at:        datetime = Field(default_factory=datetime.utcnow)


class EngineeringCompletedEvent(BaseModel):
    project_id:      str
    workflow_id:     str
    feature_name:    str
    modules_total:   int
    pull_request_id: Optional[str] = None
    merge_sha:        Optional[str] = None


# ── Errors ────────────────────────────────────────────────────

class EngineeringServiceError(Exception):
    """Base class for all Engineering Service errors."""


class CodingContractViolation(EngineeringServiceError):
    """Raised when a CodeModule fails the mandatory coding contract."""


class NoUiBlueprintError(EngineeringServiceError):
    """Raised when Frontend workers are invoked without an approved ui_blueprint."""


class ReviewGateBlockedError(EngineeringServiceError):
    """Raised when Review team blocks and no more re-run budget remains."""


class DeadLetterError(EngineeringServiceError):
    """Raised when a task exhausts its retry budget and is dead-lettered."""
