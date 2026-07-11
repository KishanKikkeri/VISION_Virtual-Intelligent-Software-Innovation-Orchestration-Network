"""
services/engineering/models — Stage 1 core models.
=====================================================
Pure in-memory/Pydantic models used across the Engineering service.

Design decision (see docs/M3.3_Engineering_Service_Handover.md):
Engineering does NOT introduce new ORM tables. `Artifact` (generic,
already in infrastructure/database/models.py) stores every artifact
type — including the new `source_code`, `implementation_plan`,
`review_report`, and `build_result` types produced here — exactly the
way Architecture and Product already store their artifacts. New
Alembic migrations are therefore not "strictly required" per the
spec's Deliverables note.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Enums ─────────────────────────────────────────────────────

class EngineeringTeam(str, Enum):
    BACKEND     = "backend"
    FRONTEND    = "frontend"
    INTEGRATION = "integration"
    REVIEW      = "review"


class ModuleType(str, Enum):
    DATABASE       = "database"
    AUTH           = "auth"
    BUSINESS_LOGIC = "business_logic"
    API_ENDPOINT   = "api_endpoint"
    COMPONENT      = "component"
    PAGE           = "page"
    STATE          = "state"
    ROUTING        = "routing"
    INTERNAL_EVENT = "internal_event"
    EXTERNAL_API   = "external_api"
    MESSAGING      = "messaging"


class EngineeringTaskStatus(str, Enum):
    PENDING       = "pending"
    RUNNING       = "running"
    COMPLETED     = "completed"
    FAILED        = "failed"
    ESCALATED     = "escalated"
    DEAD_LETTERED = "dead_lettered"


class ReviewVerdict(str, Enum):
    PASS   = "pass"
    REVISE = "revise"
    BLOCK  = "block"


# ── CodeModule ────────────────────────────────────────────────

class CodeFile(BaseModel):
    path:     str
    language: str
    content:  str


class CodeModule(BaseModel):
    """
    The unit of output produced by every Engineering worker.
    Must satisfy the Coding Contract (buildable, runnable, testable,
    traceable, deterministic, reviewable, idempotent) before it is
    eligible for the Repository Service.
    """
    module_id:      str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id:     str
    task_id:        str
    module_type:    ModuleType
    files:          List[CodeFile] = Field(default_factory=list)
    quality_score:  float = 0.0
    generated_by:   str
    review_passed:  bool = False
    idempotent_key: Optional[str] = None   # hash used for idempotent replay detection
    created_at:     datetime = Field(default_factory=datetime.utcnow)

    @property
    def is_buildable(self) -> bool:
        return bool(self.files) and self.quality_score > 0.0

    def satisfies_coding_contract(self) -> List[str]:
        """Returns a list of violated contract properties (empty == satisfies)."""
        violations = []
        if not self.files:
            violations.append("buildable")
        if self.quality_score < 0.7:
            violations.append("reviewable")
        if not self.task_id:
            violations.append("traceable")
        if not self.idempotent_key:
            violations.append("idempotent")
        return violations


# ── EngineeringTask ───────────────────────────────────────────

class EngineeringTask(BaseModel):
    """One unit of dependency-scheduled work assigned to a worker agent."""
    task_id:          str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id:        str
    team:              EngineeringTeam
    worker_agent_id:   str
    description:       str = ""
    depends_on:        List[str] = Field(default_factory=list)
    status:            EngineeringTaskStatus = EngineeringTaskStatus.PENDING
    retry_count:       int = 0
    max_retries:       int = 3
    result_module_id:  Optional[str] = None
    failure_reason:    Optional[str] = None
    escalated:         bool = False
    dead_lettered:     bool = False

    def can_run(self, completed_task_ids: set) -> bool:
        return (
            self.status == EngineeringTaskStatus.PENDING
            and all(dep in completed_task_ids for dep in self.depends_on)
        )

    def next_backoff_seconds(self) -> int:
        """Exponential backoff: 2^retry_count, capped at 60s."""
        return min(60, 2 ** max(0, self.retry_count))


# ── ImplementationPlan ───────────────────────────────────────

class ImplementationPlan(BaseModel):
    """
    Stage-1 output of task breakdown: the full dependency-scheduled
    task graph derived from approved Architecture artifacts.
    """
    plan_id:            str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id:         str
    feature_name:       str
    tasks:              List[EngineeringTask] = Field(default_factory=list)
    architecture_refs:  Dict[str, Any] = Field(default_factory=dict)
    created_at:         datetime = Field(default_factory=datetime.utcnow)

    def ready_tasks(self, completed_task_ids: set) -> List[EngineeringTask]:
        return [t for t in self.tasks if t.can_run(completed_task_ids)]

    def tasks_by_team(self, team: EngineeringTeam) -> List[EngineeringTask]:
        return [t for t in self.tasks if t.team == team]

    @property
    def all_complete(self) -> bool:
        return all(t.status == EngineeringTaskStatus.COMPLETED for t in self.tasks)

    @property
    def any_dead_lettered(self) -> bool:
        return any(t.dead_lettered for t in self.tasks)


# ── ReviewResult ──────────────────────────────────────────────

class ReviewResult(BaseModel):
    module_id:        str
    verdict:          ReviewVerdict
    score:            float
    blocking_issues:  List[str] = Field(default_factory=list)
    warnings:         List[str] = Field(default_factory=list)
    suggestions:      List[str] = Field(default_factory=list)
    reviewed_by:      str
    cycle:            int = 1


# ── BuildResult ───────────────────────────────────────────────

class BuildResult(BaseModel):
    """Final Stage-6 result: what actually landed in Repository Service."""
    project_id:          str
    feature_name:        str
    passed:               bool
    modules_checked:      int = 0
    failed_modules:       List[str] = Field(default_factory=list)
    integration_branch:   Optional[str] = None
    commit_shas:          List[str] = Field(default_factory=list)
    pull_request_id:      Optional[str] = None
    pull_request_url:     Optional[str] = None
    merged:               bool = False
    merge_sha:            Optional[str] = None
