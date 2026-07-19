"""
core/contracts/__init__.py
===========================
Sprint 2: Shared Contracts.
Every service, agent, and infrastructure module imports types from here.
Schemas are never duplicated across services.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ProjectStatus(str, Enum):
    INITIALIZING   = "initializing"
    REQUIREMENTS   = "requirements"
    ARCHITECTURE   = "architecture"
    STRUCTURE      = "structure"
    IMPLEMENTATION = "implementation"
    TESTING        = "testing"
    SECURITY       = "security"
    DEPLOYMENT     = "deployment"
    MONITORING     = "monitoring"
    IMPROVEMENT    = "improvement"
    COMPLETED      = "completed"
    FAILED         = "failed"
    PAUSED         = "paused"


class PhaseStatus(str, Enum):
    PENDING           = "pending"
    RUNNING           = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED          = "approved"
    REJECTED          = "rejected"
    COMPLETED         = "completed"
    FAILED            = "failed"


class ArtifactStatus(str, Enum):
    DRAFT        = "draft"
    UNDER_REVIEW = "under_review"
    APPROVED     = "approved"
    REJECTED     = "rejected"
    SUPERSEDED   = "superseded"


class TaskStatus(str, Enum):
    PENDING       = "pending"
    RUNNING       = "running"
    COMPLETED     = "completed"
    FAILED        = "failed"
    ESCALATED     = "escalated"
    DEAD_LETTERED = "dead_lettered"


class UserRole(str, Enum):
    OWNER     = "owner"
    ADMIN     = "admin"
    DEVELOPER = "developer"
    REVIEWER  = "reviewer"
    OBSERVER  = "observer"


class LLMProvider(str, Enum):
    ANTHROPIC  = "anthropic"
    OPENAI     = "openai"
    GEMINI     = "gemini"
    OLLAMA     = "ollama"
    OPENROUTER = "openrouter"


class FinishReason(str, Enum):
    STOP           = "stop"
    MAX_TOKENS     = "max_tokens"
    CONTENT_FILTER = "content_filter"
    ERROR          = "error"


# ── LLM contracts ─────────────────────────────────────────────

class LLMMessage(BaseModel):
    role:    str
    content: str

    def to_dict(self) -> Dict[str, str]:
        return {"role": self.role, "content": self.content}


class LLMRequest(BaseModel):
    messages:    List[LLMMessage]
    provider:    LLMProvider
    model:       str
    max_tokens:  int   = 4096
    temperature: float = 0.2
    project_id:  Optional[str] = None
    agent_id:    Optional[str] = None
    task_id:     Optional[str] = None


class LLMResponse(BaseModel):
    content:       str
    input_tokens:  int
    output_tokens: int
    total_tokens:  int
    model:         str
    provider:      LLMProvider
    finish_reason: FinishReason
    latency_ms:    int
    cost_usd:      float


class TokenUsageRecord(BaseModel):
    project_id:    str
    agent_run_id:  Optional[str] = None
    agent_id:      str
    department:    str
    provider:      str
    model:         str
    input_tokens:  int
    output_tokens: int
    cost_usd:      float
    recorded_at:   datetime = Field(default_factory=datetime.utcnow)


# ── Event contracts ───────────────────────────────────────────

class NATSEvent(BaseModel):
    subject:    str
    payload:    Dict[str, Any]
    project_id: Optional[str] = None
    event_id:   str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp:  datetime = Field(default_factory=datetime.utcnow)


class WebSocketEvent(BaseModel):
    project_id: str
    event_type: str
    payload:    Dict[str, Any]
    timestamp:  datetime = Field(default_factory=datetime.utcnow)


class AuditEventRecord(BaseModel):
    project_id:  Optional[str] = None
    event_type:  str
    actor_type:  str
    actor_id:    str
    entity_type: Optional[str] = None
    entity_id:   Optional[str] = None
    payload:     Dict[str, Any] = Field(default_factory=dict)
    recorded_at: datetime = Field(default_factory=datetime.utcnow)


# ── Artifact contracts ────────────────────────────────────────

class ArtifactRef(BaseModel):
    artifact_id:   str
    artifact_type: str
    version:       int
    storage_ref:   Optional[str] = None


class CreateArtifactRequest(BaseModel):
    project_id:    str
    artifact_type: str
    created_by:    str
    content:       Optional[Any] = None
    storage_ref:   Optional[str] = None
    metadata:      Dict[str, Any] = Field(default_factory=dict)


# ── Agent contracts ───────────────────────────────────────────

class AgentMessage(BaseModel):
    message_id:   str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id:      str
    project_id:   str
    agent_id:     str
    parent_agent: Optional[str] = None
    department:   str
    status:       TaskStatus
    artifacts:    List[ArtifactRef] = Field(default_factory=list)
    feedback:     List[str] = Field(default_factory=list)
    retry_count:  int = 0
    timestamp:    datetime = Field(default_factory=datetime.utcnow)


class AgentResult(BaseModel):
    task_id:       str
    agent_id:      str
    status:        TaskStatus
    content:       Optional[Any] = None
    summary:       str = ""
    quality_score: float = 0.0
    artifacts:     List[ArtifactRef] = Field(default_factory=list)
    token_usage:   Optional[TokenUsageRecord] = None
    nats_events:   List[NATSEvent] = Field(default_factory=list)
    ws_events:     List[WebSocketEvent] = Field(default_factory=list)
    failure_reason:Optional[str] = None
    duration_ms:   int = 0


# ── Workflow contracts ────────────────────────────────────────

class WorkflowState(BaseModel):
    project_id:     str
    workflow_id:    str
    current_phase:  int = 1
    phase_status:   PhaseStatus = PhaseStatus.PENDING
    artifacts:      Dict[str, str] = Field(default_factory=dict)
    approval_status:Optional[str] = None
    budget_status:  str = "active"
    total_spend_usd:float = 0.0
    retry_count:    int = 0
    failure_reason: Optional[str] = None


# ── API response contracts ────────────────────────────────────

class ProjectSummary(BaseModel):
    id:            str
    name:          str
    status:        ProjectStatus
    current_phase: int
    owner_id:      str
    created_at:    datetime


class HealthStatus(BaseModel):
    status:      str
    version:     str = "1.0.0"
    environment: str
    checks:      Dict[str, bool] = Field(default_factory=dict)
    timestamp:   datetime = Field(default_factory=datetime.utcnow)
