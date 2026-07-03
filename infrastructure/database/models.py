"""
infrastructure/database/models.py
===================================
SQLAlchemy 2 ORM models.
Maps directly to the PostgreSQL schema defined in AASC_PostgreSQL_Schema_v1.sql.
Every model follows the same conventions:
  - UUID primary keys (server default gen_random_uuid())
  - TIMESTAMPTZ for all timestamps
  - JSONB for variable-length structured data
  - Append-only tables documented with a class-level comment
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey,
    Integer, Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from infrastructure.database.connection import Base


# ── helpers ───────────────────────────────────────────────────

def _uuid() -> str:
    return str(uuid.uuid4())

def _now() -> datetime:
    return datetime.utcnow()


# ═══════════════════════════════════════════════════════════════
# CROSS-CUTTING TABLES
# ═══════════════════════════════════════════════════════════════

class User(Base):
    __tablename__ = "users"

    id:            Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    email:         Mapped[str]           = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str]           = mapped_column(String(255), nullable=False)
    full_name:     Mapped[Optional[str]] = mapped_column(String(255))
    role:          Mapped[str]           = mapped_column(String(50), nullable=False, default="developer")
    is_active:     Mapped[bool]          = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at:    Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at:    Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("role IN ('owner','admin','developer','reviewer','observer')", name="ck_users_role"),
    )


class Agent(Base):
    __tablename__ = "agents"

    id:              Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    agent_id:        Mapped[str]           = mapped_column(String(100), unique=True, nullable=False)
    name:            Mapped[str]           = mapped_column(String(255), nullable=False)
    department:      Mapped[str]           = mapped_column(String(100), nullable=False)
    layer:           Mapped[int]           = mapped_column(Integer, nullable=False)
    role:            Mapped[str]           = mapped_column(String(50), nullable=False)
    parent_agent_id: Mapped[Optional[str]] = mapped_column(String(100), ForeignKey("agents.agent_id", ondelete="SET NULL"))
    default_provider:Mapped[Optional[str]] = mapped_column(String(50))
    default_model:   Mapped[Optional[str]] = mapped_column(String(100))
    is_active:       Mapped[bool]          = mapped_column(Boolean, nullable=False, default=True)
    created_at:      Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("layer IN (2,3,4,5)", name="ck_agents_layer"),
        CheckConstraint("role IN ('manager','head','lead','worker')", name="ck_agents_role"),
    )


class AgentPrompt(Base):
    __tablename__ = "agent_prompts"

    id:                   Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    agent_id:             Mapped[str]           = mapped_column(String(100), ForeignKey("agents.agent_id", ondelete="CASCADE"), nullable=False)
    version:              Mapped[int]           = mapped_column(Integer, nullable=False, default=1)
    system_prompt:        Mapped[str]           = mapped_column(Text, nullable=False)
    user_prompt_template: Mapped[Optional[str]] = mapped_column(Text)
    is_active:            Mapped[bool]          = mapped_column(Boolean, nullable=False, default=True)
    created_at:           Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("agent_id", "version", name="uq_agent_prompts_agent_version"),
    )


class Artifact(Base):
    __tablename__ = "artifacts"

    id:            Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id:    Mapped[str]           = mapped_column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    artifact_type: Mapped[str]           = mapped_column(String(100), nullable=False)
    version:       Mapped[int]           = mapped_column(Integer, nullable=False, default=1)
    created_by:    Mapped[str]           = mapped_column(String(100), nullable=False)
    approved_by:   Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"))
    status:        Mapped[str]           = mapped_column(String(50), nullable=False, default="draft")
    storage_ref:   Mapped[Optional[str]] = mapped_column(Text)
    content:       Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)
    metadata_:     Mapped[Dict[str, Any]]= mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_at:    Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at:    Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('draft','under_review','approved','rejected','superseded')", name="ck_artifacts_status"),
        UniqueConstraint("project_id", "artifact_type", "version", name="uq_artifacts_project_type_version"),
    )


class TokenLedger(Base):
    """APPEND-ONLY. No UPDATE or DELETE permitted."""
    __tablename__ = "token_ledger"

    id:           Mapped[str]   = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id:   Mapped[str]   = mapped_column(String(36), ForeignKey("projects.id"), nullable=False)
    agent_run_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("agent_runs.id", ondelete="SET NULL"))
    agent_id:     Mapped[str]   = mapped_column(String(100), nullable=False)
    department:   Mapped[str]   = mapped_column(String(100), nullable=False)
    provider:     Mapped[str]   = mapped_column(String(50), nullable=False)
    model:        Mapped[str]   = mapped_column(String(100), nullable=False)
    input_tokens: Mapped[int]   = mapped_column(Integer, nullable=False)
    output_tokens:Mapped[int]   = mapped_column(Integer, nullable=False)
    cost_usd:     Mapped[float] = mapped_column(Numeric(12, 6), nullable=False)
    recorded_at:  Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditEvent(Base):
    """APPEND-ONLY. No UPDATE or DELETE permitted. The black box recorder."""
    __tablename__ = "audit_events"

    id:          Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id:  Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("projects.id", ondelete="SET NULL"))
    event_type:  Mapped[str]           = mapped_column(String(100), nullable=False)
    actor_type:  Mapped[str]           = mapped_column(String(50), nullable=False)
    actor_id:    Mapped[str]           = mapped_column(String(255), nullable=False)
    entity_type: Mapped[Optional[str]] = mapped_column(String(100))
    entity_id:   Mapped[Optional[str]] = mapped_column(String(36))
    payload:     Mapped[Dict[str, Any]]= mapped_column(JSONB, nullable=False, default=dict)
    recorded_at: Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("actor_type IN ('user','agent','system')", name="ck_audit_actor_type"),
    )


# ═══════════════════════════════════════════════════════════════
# MANAGER-SERVICE TABLES
# ═══════════════════════════════════════════════════════════════

class Project(Base):
    __tablename__ = "projects"

    id:            Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    name:          Mapped[str]           = mapped_column(String(255), nullable=False)
    description:   Mapped[str]           = mapped_column(Text, nullable=False)
    status:        Mapped[str]           = mapped_column(String(50), nullable=False, default="initializing")
    current_phase: Mapped[int]           = mapped_column(Integer, nullable=False, default=1)
    owner_id:      Mapped[str]           = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    repository_url:Mapped[Optional[str]] = mapped_column(Text)
    llm_provider:  Mapped[str]           = mapped_column(String(50), nullable=False, default="anthropic")
    created_at:    Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at:    Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    workflows:    Mapped[List["Workflow"]]   = relationship("Workflow", back_populates="project")
    budget_limit: Mapped[Optional["BudgetLimit"]] = relationship("BudgetLimit", back_populates="project", uselist=False)
    artifacts:    Mapped[List["Artifact"]]   = relationship("Artifact", foreign_keys="Artifact.project_id")
    agent_runs:   Mapped[List["AgentRun"]]   = relationship("AgentRun", back_populates="project")


class BudgetLimit(Base):
    __tablename__ = "budget_limits"

    id:                   Mapped[str]            = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id:           Mapped[str]            = mapped_column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, unique=True)
    limit_usd:            Mapped[Optional[float]]= mapped_column(Numeric(10, 2))
    warning_threshold_pct:Mapped[int]            = mapped_column(Integer, nullable=False, default=80)
    status:               Mapped[str]            = mapped_column(String(50), nullable=False, default="active")
    created_at:           Mapped[datetime]       = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at:           Mapped[datetime]       = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    project: Mapped["Project"] = relationship("Project", back_populates="budget_limit")


class Workflow(Base):
    __tablename__ = "workflows"

    id:            Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id:    Mapped[str]           = mapped_column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, unique=True)
    current_phase: Mapped[int]           = mapped_column(Integer, nullable=False, default=1)
    status:        Mapped[str]           = mapped_column(String(50), nullable=False, default="active")
    started_at:    Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at:  Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at:    Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())

    project: Mapped["Project"] = relationship("Project", back_populates="workflows")
    phases:  Mapped[List["WorkflowPhase"]] = relationship("WorkflowPhase", back_populates="workflow")


class WorkflowPhase(Base):
    __tablename__ = "workflow_phases"

    id:                Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    workflow_id:       Mapped[str]           = mapped_column(String(36), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False)
    project_id:        Mapped[str]           = mapped_column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    phase_number:      Mapped[int]           = mapped_column(Integer, nullable=False)
    phase_name:        Mapped[str]           = mapped_column(String(100), nullable=False)
    status:            Mapped[str]           = mapped_column(String(50), nullable=False, default="pending")
    requires_approval: Mapped[bool]          = mapped_column(Boolean, nullable=False, default=False)
    started_at:        Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at:      Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    approved_by:       Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"))
    approved_at:       Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    rejection_feedback:Mapped[Optional[str]] = mapped_column(Text)
    revision_round:    Mapped[int]           = mapped_column(Integer, nullable=False, default=0)
    created_at:        Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())

    workflow: Mapped["Workflow"] = relationship("Workflow", back_populates="phases")

    __table_args__ = (
        UniqueConstraint("workflow_id", "phase_number", name="uq_workflow_phases_workflow_phase"),
    )


class Approval(Base):
    __tablename__ = "approvals"

    id:                Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id:        Mapped[str]           = mapped_column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    workflow_phase_id: Mapped[str]           = mapped_column(String(36), ForeignKey("workflow_phases.id"), nullable=False)
    artifact_type:     Mapped[str]           = mapped_column(String(100), nullable=False)
    artifact_ids:      Mapped[List[str]]     = mapped_column(ARRAY(String), nullable=False, default=list)
    status:            Mapped[str]           = mapped_column(String(50), nullable=False, default="pending")
    requested_at:      Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())
    responded_at:      Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    responded_by:      Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"))
    feedback:          Mapped[Optional[str]] = mapped_column(Text)
    revision_round:    Mapped[int]           = mapped_column(Integer, nullable=False, default=1)
    expires_at:        Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at:        Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id:              Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id:      Mapped[str]           = mapped_column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    task_id:         Mapped[str]           = mapped_column(String(36), nullable=False)
    agent_id:        Mapped[str]           = mapped_column(String(100), ForeignKey("agents.agent_id"), nullable=False)
    parent_agent_id: Mapped[Optional[str]] = mapped_column(String(100), ForeignKey("agents.agent_id"))
    department:      Mapped[str]           = mapped_column(String(100), nullable=False)
    status:          Mapped[str]           = mapped_column(String(50), nullable=False, default="pending")
    input_context:   Mapped[Dict[str, Any]]= mapped_column(JSONB, nullable=False, default=dict)
    output_data:     Mapped[Dict[str, Any]]= mapped_column(JSONB, nullable=False, default=dict)
    retry_count:     Mapped[int]           = mapped_column(Integer, nullable=False, default=0)
    error_message:   Mapped[Optional[str]] = mapped_column(Text)
    started_at:      Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at:    Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    duration_ms:     Mapped[Optional[int]] = mapped_column(Integer)
    created_at:      Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())

    project: Mapped["Project"] = relationship("Project", back_populates="agent_runs")
