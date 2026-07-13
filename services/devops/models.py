"""
services/devops/models.py — M3.6 DevOps Service core models.
================================================================
Flat module (not a package), per the M3.6 spec's explicit deliverables
layout — unlike QA (M3.4) and Security (M3.5), which predate this
locked spec and use `models/__init__.py` packages.

Design decision (see docs/M3.6_DevOps_Service_Handover.md): unlike QA/
Security, DevOps gets first-class ORM tables (infrastructure/database/
models.py: Deployment, DeploymentHistory, DeploymentHealth,
ReleaseMetadata, RollbackRecord) per the spec's explicit "Additional
deliverables: Alembic migration(s), Database models" instruction. These
Pydantic models are the in-memory/API shapes used across workers, leads,
and head; services/devops/integration/deployment_repository.py maps
between them and the ORM rows.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# -- Enums --------------------------------------------------------

class WorkerTeam(str, Enum):
    INFRASTRUCTURE = "infrastructure"   # container_lead: dockerfile + compose
    CICD           = "cicd"             # cicd_lead: pipeline + environment config
    DEPLOYMENT     = "deployment"       # infrastructure_ops_lead: provision + health


class DeploymentStatus(str, Enum):
    PENDING           = "pending"
    AWAITING_APPROVAL = "awaiting_approval"
    DEPLOYING         = "deploying"
    HEALTHY           = "healthy"
    FAILED            = "failed"
    ROLLED_BACK       = "rolled_back"


class DevOpsTaskStatus(str, Enum):
    PENDING       = "pending"
    RUNNING       = "running"
    COMPLETED     = "completed"
    FAILED        = "failed"
    ESCALATED     = "escalated"
    DEAD_LETTERED = "dead_lettered"


class RollbackStatus(str, Enum):
    INITIATED = "initiated"
    COMPLETED = "completed"
    FAILED    = "failed"


class VersionBump(str, Enum):
    MAJOR = "major"
    MINOR = "minor"
    PATCH = "patch"


# -- Infrastructure artifacts (Container Lead / container_lead) --

class DockerfileArtifact(BaseModel):
    project_id:    str
    content:       str
    base_image:    str = "python:3.12-slim"
    exposed_port:  int = 8000


class ComposeArtifact(BaseModel):
    project_id: str
    content:    str
    services:   List[str] = Field(default_factory=list)


# -- CI/CD artifacts (cicd_lead) -----------------------------------

class PipelineConfigArtifact(BaseModel):
    """
    GitHub Actions workflow — cicd_lead's pipeline_config_worker owns
    both the spec's "GitHub Actions Worker" and "Pipeline Worker"
    responsibilities (see docs/M3.6 handover Department Structure note).
    """
    project_id:    str
    content:       str
    workflow_name: str = "build.yml"


class EnvironmentConfigArtifact(BaseModel):
    project_id: str
    content:    str                          # rendered .env.example text
    variables:  Dict[str, str] = Field(default_factory=dict)


# -- Deployment plan (pre-approval output, reviewed by Manager) --

class DeploymentPlan(BaseModel):
    plan_id:            str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id:         str
    target_environment: str = "production"
    proposed_version:   str = "0.1.0"
    qa_verdict:         str = "unknown"
    security_verdict:   str = "unknown"
    risk_level:         str = "low"
    dockerfile_ref:     Optional[str] = None
    compose_ref:        Optional[str] = None
    pipeline_ref:       Optional[str] = None
    environment_ref:    Optional[str] = None
    blocking_reasons:   List[str] = Field(default_factory=list)
    created_at:         datetime = Field(default_factory=datetime.utcnow)

    @property
    def ready_for_approval(self) -> bool:
        return not self.blocking_reasons


# -- Health ---------------------------------------------------------

class HealthCheckResult(BaseModel):
    check_name: str
    passed:     bool
    detail:     str = ""


# Per spec's "Health Validation" section — a deployment succeeds only
# if ALL of these pass.
REQUIRED_HEALTH_CHECKS = (
    "service_reachable", "rest_health_endpoint", "database_connected",
    "nats_connected", "websocket_connected", "startup_successful",
)


class HealthReport(BaseModel):
    project_id:    str
    deployment_id: str
    checks:        List[HealthCheckResult] = Field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    @property
    def failed_checks(self) -> List[str]:
        return [c.check_name for c in self.checks if not c.passed]


# -- Rollback ---------------------------------------------------------

# Per spec's "Rollback Policy" section — any of these triggers an
# automatic rollback.
ROLLBACK_TRIGGERS = (
    "deployment_failure", "health_failure", "startup_timeout",
    "migration_failure", "container_crash", "dependency_unavailable",
)


class RollbackReport(BaseModel):
    report_id:              str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id:             str
    deployment_id:          str
    reason:                 str
    rolled_back_to_version: Optional[str] = None
    status:                 RollbackStatus = RollbackStatus.INITIATED
    created_at:             datetime = Field(default_factory=datetime.utcnow)


# -- Release ------------------------------------------------------------

class Release(BaseModel):
    release_id:       str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id:        str
    version:            str
    previous_version:   Optional[str] = None
    release_notes:      str = ""
    released_at:        datetime = Field(default_factory=datetime.utcnow)


# -- Final deployment report (Release Lead's summary output) ------------

class DeploymentReport(BaseModel):
    report_id:      str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id:      str
    deployment_id:   str
    status:          DeploymentStatus = DeploymentStatus.FAILED
    version:         Optional[str] = None
    health_report:   Optional[HealthReport] = None
    rollback_report: Optional[RollbackReport] = None
    release:         Optional[Release] = None
    blocking_reasons: List[str] = Field(default_factory=list)
    created_at:      datetime = Field(default_factory=datetime.utcnow)

    @property
    def succeeded(self) -> bool:
        return self.status == DeploymentStatus.HEALTHY


# -- DevOps task / plan (mirrors SecurityTask / SecurityPlan) --------

class DevOpsTask(BaseModel):
    task_id:         str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id:      str
    team:            WorkerTeam
    worker_agent_id: str
    description:     str = ""
    depends_on:      List[str] = Field(default_factory=list)
    status:          DevOpsTaskStatus = DevOpsTaskStatus.PENDING
    retry_count:     int = 0
    max_retries:     int = 3
    failure_reason:  Optional[str] = None
    escalated:       bool = False
    dead_lettered:   bool = False

    def can_run(self, completed_task_ids: set) -> bool:
        return (
            self.status == DevOpsTaskStatus.PENDING
            and all(dep in completed_task_ids for dep in self.depends_on)
        )

    def next_backoff_seconds(self) -> int:
        return min(60, 2 ** max(0, self.retry_count))


class DevOpsPlan(BaseModel):
    plan_id:          str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id:        str
    feature_name:       str
    tasks:              List[DevOpsTask] = Field(default_factory=list)
    upstream_refs:       Dict[str, Any] = Field(default_factory=dict)
    created_at:          datetime = Field(default_factory=datetime.utcnow)

    def ready_tasks(self, completed_task_ids: set) -> List[DevOpsTask]:
        return [t for t in self.tasks if t.can_run(completed_task_ids)]

    def tasks_by_team(self, team: WorkerTeam) -> List[DevOpsTask]:
        return [t for t in self.tasks if t.team == team]

    @property
    def all_complete(self) -> bool:
        return all(t.status == DevOpsTaskStatus.COMPLETED for t in self.tasks)

    @property
    def any_dead_lettered(self) -> bool:
        return any(t.dead_lettered for t in self.tasks)
