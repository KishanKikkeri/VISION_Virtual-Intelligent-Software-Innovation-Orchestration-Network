"""
services/devops/schemas.py — API & event contracts for M3.6.
================================================================
Nothing outside this module should define ad-hoc request/response
dict shapes for the DevOps Service's HTTP API or NATS events.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from services.devops.models import (
    DeploymentPlan,
    DeploymentReport,
    DeploymentStatus,
    DevOpsTaskStatus,
    HealthReport,
    RollbackReport,
)


# -- Requests ---------------------------------------------------

class DeployRequest(BaseModel):
    project_id:   str
    workflow_id:  str
    feature_name: str = "default"


class RollbackRequest(BaseModel):
    project_id: str
    reason:     str


class ApproveDeploymentRequest(BaseModel):
    project_id:  str
    approved:    bool
    approved_by: Optional[str] = None
    feedback:    Optional[str] = None


# -- Responses ----------------------------------------------------

class DevOpsTaskResponse(BaseModel):
    task_id:         str
    team:            str
    worker_agent_id: str
    status:          DevOpsTaskStatus
    retry_count:     int
    depends_on:      List[str]
    failure_reason:  Optional[str] = None


class DevOpsPlanResponse(BaseModel):
    plan_id:      str
    project_id:   str
    feature_name: str
    total_tasks:  int
    tasks:        List[DevOpsTaskResponse]


class DeploymentStatusResponse(BaseModel):
    project_id:        str
    deployment_id:     Optional[str] = None
    phase_status:       str
    status:             DeploymentStatus = DeploymentStatus.PENDING
    deployment_plan:     Optional[DeploymentPlan] = None
    health_report:       Optional[HealthReport] = None
    rollback_report:     Optional[RollbackReport] = None
    deployment_report:   Optional[DeploymentReport] = None
    updated_at:          datetime = Field(default_factory=datetime.utcnow)


class DevOpsCompletedEvent(BaseModel):
    project_id:    str
    workflow_id:   str
    feature_name:  str
    passed:        bool
    status:        str
    version:       Optional[str] = None


# -- Errors ---------------------------------------------------------

class DevOpsServiceError(Exception):
    """Base class for all DevOps Service errors."""


class NoValidatedArtifactsError(DevOpsServiceError):
    """Raised when DevOps is invoked without approved QA/Security reports."""


class DeploymentBlockedError(DevOpsServiceError):
    """Raised when the deployment gate blocks and no more retry budget remains."""


class HealthCheckFailedError(DevOpsServiceError):
    """Raised when post-deploy health validation fails and rollback is triggered."""


class RollbackFailedError(DevOpsServiceError):
    """Raised when an automatic rollback itself fails — requires manual intervention."""


class DeadLetterError(DevOpsServiceError):
    """Raised when a DevOps task exhausts its retry budget and is dead-lettered."""
