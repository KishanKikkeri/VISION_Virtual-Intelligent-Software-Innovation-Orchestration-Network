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


# ═══════════════════════════════════════════════════════════════
# M3.2 — REPOSITORY SERVICE TABLES
# ═══════════════════════════════════════════════════════════════

class Repository(Base):
    """
    A single Git repository (monorepo) managed on behalf of a project.
    One row per project — Repository Service enforces 1:1 project↔repo.
    """
    __tablename__ = "repositories"

    id:            Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id:    Mapped[str]           = mapped_column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, unique=True)
    provider:      Mapped[str]           = mapped_column(String(50), nullable=False, default="github")
    owner:         Mapped[str]           = mapped_column(String(255), nullable=False)
    name:          Mapped[str]           = mapped_column(String(255), nullable=False)
    full_name:     Mapped[str]           = mapped_column(String(511), nullable=False)
    default_branch:Mapped[str]           = mapped_column(String(255), nullable=False, default="main")
    clone_url:     Mapped[Optional[str]] = mapped_column(Text)
    html_url:      Mapped[Optional[str]] = mapped_column(Text)
    visibility:    Mapped[str]           = mapped_column(String(20), nullable=False, default="private")
    status:        Mapped[str]           = mapped_column(String(50), nullable=False, default="active")
    provider_repo_id: Mapped[Optional[str]] = mapped_column(String(100))
    metadata_:     Mapped[Dict[str, Any]]= mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_at:    Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at:    Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    branches:      Mapped[List["Branch"]]       = relationship("Branch", back_populates="repository")
    pull_requests: Mapped[List["PullRequest"]]  = relationship("PullRequest", back_populates="repository")

    __table_args__ = (
        CheckConstraint("provider IN ('github','gitlab','bitbucket','azure_devops')", name="ck_repositories_provider"),
        CheckConstraint("visibility IN ('private','internal','public')", name="ck_repositories_visibility"),
        CheckConstraint("status IN ('active','archived','deleted')", name="ck_repositories_status"),
        UniqueConstraint("owner", "name", name="uq_repositories_owner_name"),
    )


class Branch(Base):
    """A branch within a managed repository."""
    __tablename__ = "branches"

    id:            Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    repository_id: Mapped[str]           = mapped_column(String(36), ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False)
    name:          Mapped[str]           = mapped_column(String(255), nullable=False)
    branch_type:   Mapped[str]           = mapped_column(String(50), nullable=False, default="feature")
    task_id:       Mapped[Optional[str]] = mapped_column(String(100))
    base_branch:   Mapped[str]           = mapped_column(String(255), nullable=False, default="develop")
    head_sha:      Mapped[Optional[str]] = mapped_column(String(64))
    is_protected:  Mapped[bool]          = mapped_column(Boolean, nullable=False, default=False)
    status:        Mapped[str]           = mapped_column(String(50), nullable=False, default="active")
    created_by:    Mapped[str]           = mapped_column(String(100), nullable=False, default="VISION Bot")
    created_at:    Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())
    merged_at:     Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    deleted_at:    Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    repository:    Mapped["Repository"] = relationship("Repository", back_populates="branches")

    __table_args__ = (
        CheckConstraint("branch_type IN ('protected','feature','fix','hotfix')", name="ck_branches_type"),
        CheckConstraint("status IN ('active','merged','deleted')", name="ck_branches_status"),
        UniqueConstraint("repository_id", "name", name="uq_branches_repository_name"),
    )


class PullRequest(Base):
    """A pull request opened by Repository Service on behalf of Engineering."""
    __tablename__ = "pull_requests"

    id:              Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    repository_id:   Mapped[str]           = mapped_column(String(36), ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False)
    provider_pr_number: Mapped[Optional[int]] = mapped_column(Integer)
    task_id:         Mapped[Optional[str]] = mapped_column(String(100))
    title:           Mapped[str]           = mapped_column(String(500), nullable=False)
    description:     Mapped[Optional[str]] = mapped_column(Text)
    source_branch:   Mapped[str]           = mapped_column(String(255), nullable=False)
    target_branch:   Mapped[str]           = mapped_column(String(255), nullable=False, default="develop")
    status:          Mapped[str]           = mapped_column(String(50), nullable=False, default="open")
    merge_strategy:  Mapped[str]           = mapped_column(String(50), nullable=False, default="squash")
    reviewers:       Mapped[List[str]]     = mapped_column(ARRAY(String), nullable=False, default=list)
    merge_sha:       Mapped[Optional[str]] = mapped_column(String(64))
    html_url:        Mapped[Optional[str]] = mapped_column(Text)
    opened_at:       Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())
    approved_at:     Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    merged_at:       Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    closed_at:       Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at:      Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())

    repository:      Mapped["Repository"] = relationship("Repository", back_populates="pull_requests")

    __table_args__ = (
        CheckConstraint("status IN ('open','approved','merged','closed','conflicted')", name="ck_pull_requests_status"),
        CheckConstraint("merge_strategy IN ('squash')", name="ck_pull_requests_merge_strategy"),
    )


class RepositoryEvent(Base):
    """APPEND-ONLY. Compliance/audit log for every repository-service operation."""
    __tablename__ = "repository_events"

    id:            Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    repository_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("repositories.id", ondelete="SET NULL"))
    project_id:    Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("projects.id", ondelete="SET NULL"))
    event_type:    Mapped[str]           = mapped_column(String(100), nullable=False)
    entity_type:   Mapped[Optional[str]] = mapped_column(String(50))
    entity_id:     Mapped[Optional[str]] = mapped_column(String(100))
    actor:         Mapped[str]           = mapped_column(String(100), nullable=False, default="VISION Bot")
    payload:       Mapped[Dict[str, Any]]= mapped_column(JSONB, nullable=False, default=dict)
    recorded_at:   Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())


# ═══════════════════════════════════════════════════════════════
# M3.6 — DEVOPS SERVICE
# ═══════════════════════════════════════════════════════════════
# Unlike Product/Architecture/Engineering/QA/Security (which store all
# report-shaped output in the generic `Artifact` table), DevOps gets
# first-class tables per the M3.6 spec's explicit "Additional
# deliverables: Alembic migration(s), Database models" requirement —
# deployments/rollbacks benefit from real queryable history in a way a
# generic JSON blob doesn't. See docs/M3.6_DevOps_Service_Handover.md.

class Deployment(Base):
    """One row per deployment attempt (a single pass through the DevOps pipeline)."""
    __tablename__ = "deployments"

    id:               Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id:       Mapped[str]           = mapped_column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    workflow_id:      Mapped[Optional[str]] = mapped_column(String(36))
    environment:      Mapped[str]           = mapped_column(String(50), nullable=False, default="production")
    version:          Mapped[Optional[str]] = mapped_column(String(50))
    status:           Mapped[str]           = mapped_column(String(50), nullable=False, default="pending")
    deployment_plan_ref: Mapped[Optional[str]] = mapped_column(String(36))
    triggered_by:     Mapped[str]           = mapped_column(String(100), nullable=False, default="manager_agent")
    approved_by:      Mapped[Optional[str]] = mapped_column(String(100))
    failure_reason:   Mapped[Optional[str]] = mapped_column(Text)
    started_at:       Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at:     Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at:       Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','awaiting_approval','deploying','healthy','failed','rolled_back')",
            name="ck_deployments_status"),
    )


class DeploymentHistory(Base):
    """APPEND-ONLY. Every state transition a deployment goes through."""
    __tablename__ = "deployment_history"

    id:            Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    deployment_id: Mapped[str]           = mapped_column(String(36), ForeignKey("deployments.id", ondelete="CASCADE"), nullable=False)
    project_id:    Mapped[str]           = mapped_column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    event_type:    Mapped[str]           = mapped_column(String(100), nullable=False)
    status:        Mapped[str]           = mapped_column(String(50), nullable=False)
    payload:       Mapped[Dict[str, Any]]= mapped_column(JSONB, nullable=False, default=dict)
    recorded_at:   Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())


class DeploymentHealth(Base):
    """One row per individual health check run against a deployment."""
    __tablename__ = "deployment_health"

    id:            Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    deployment_id: Mapped[str]           = mapped_column(String(36), ForeignKey("deployments.id", ondelete="CASCADE"), nullable=False)
    project_id:    Mapped[str]           = mapped_column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    check_name:    Mapped[str]           = mapped_column(String(100), nullable=False)
    passed:        Mapped[bool]          = mapped_column(Boolean, nullable=False, default=False)
    detail:        Mapped[Optional[str]] = mapped_column(Text)
    checked_at:    Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReleaseMetadata(Base):
    """One row per released semantic version for a project."""
    __tablename__ = "release_metadata"

    id:                Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id:        Mapped[str]           = mapped_column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    deployment_id:     Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("deployments.id", ondelete="SET NULL"))
    version:           Mapped[str]           = mapped_column(String(50), nullable=False)
    previous_version:  Mapped[Optional[str]] = mapped_column(String(50))
    release_notes:     Mapped[Optional[str]] = mapped_column(Text)
    released_by:       Mapped[str]           = mapped_column(String(100), nullable=False, default="VISION Bot")
    released_at:       Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("project_id", "version", name="uq_release_metadata_project_version"),
    )


class RollbackRecord(Base):
    """APPEND-ONLY. One row per rollback triggered against a deployment."""
    __tablename__ = "rollback_records"

    id:                     Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    deployment_id:          Mapped[str]           = mapped_column(String(36), ForeignKey("deployments.id", ondelete="CASCADE"), nullable=False)
    project_id:             Mapped[str]           = mapped_column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    reason:                 Mapped[str]           = mapped_column(Text, nullable=False)
    rolled_back_to_version: Mapped[Optional[str]] = mapped_column(String(50))
    status:                 Mapped[str]           = mapped_column(String(50), nullable=False, default="initiated")
    initiated_at:           Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at:           Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("status IN ('initiated','completed','failed')", name="ck_rollback_records_status"),
    )


# ═══════════════════════════════════════════════════════════════
# MONITORING SERVICE (M3.7) — additive only, no existing table touched.
# Platform-wide by nature: project_id is nullable everywhere here
# (unlike DevOps's project-scoped tables) — a component_score for
# "postgres" or "nats" isn't owned by any one project. Where a row
# genuinely can be tied to a project (e.g. a deployment-health alert),
# project_id is populated; otherwise it stays NULL.
# See docs/M3.7_Monitoring_Service_Specification_v1.md §9.
# ═══════════════════════════════════════════════════════════════

class Metric(Base):
    """One row per distinct metric definition (name+component+unit)."""
    __tablename__ = "metrics"

    id:            Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    name:          Mapped[str]           = mapped_column(String(150), nullable=False)
    component:     Mapped[str]           = mapped_column(String(50), nullable=False)
    unit:          Mapped[Optional[str]] = mapped_column(String(30))
    created_at:    Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("name", "component", name="uq_metrics_name_component"),
    )


class MetricSample(Base):
    """APPEND-ONLY. One row per collected sample of a metric."""
    __tablename__ = "metric_samples"

    id:            Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    metric_id:     Mapped[str]           = mapped_column(String(36), ForeignKey("metrics.id", ondelete="CASCADE"), nullable=False)
    project_id:    Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("projects.id", ondelete="SET NULL"))
    value:         Mapped[float]         = mapped_column(Numeric, nullable=False)
    labels:        Mapped[Dict[str, Any]]= mapped_column(JSONB, nullable=False, default=dict)
    sampled_at:    Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())


class SystemHealth(Base):
    """APPEND-ONLY. One row per monitoring cycle's composite health score."""
    __tablename__ = "system_health"

    id:               Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    health_score:     Mapped[float]         = mapped_column(Numeric, nullable=False)
    status:           Mapped[str]           = mapped_column(String(20), nullable=False)
    component_scores: Mapped[Dict[str, Any]]= mapped_column(JSONB, nullable=False, default=dict)
    cycle_at:         Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('healthy','warning','critical')", name="ck_system_health_status"),
    )


class Alert(Base):
    """One row per open/tracked alert. Updated in place as severity/status change (alert_history is the append-only trail)."""
    __tablename__ = "alerts"

    id:            Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id:    Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("projects.id", ondelete="SET NULL"))
    component:     Mapped[str]           = mapped_column(String(50), nullable=False)
    severity:      Mapped[str]           = mapped_column(String(20), nullable=False)
    message:       Mapped[str]           = mapped_column(Text, nullable=False)
    status:        Mapped[str]           = mapped_column(String(20), nullable=False, default="open")
    first_seen_at: Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at:  Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("severity IN ('info','warning','critical')", name="ck_alerts_severity"),
        CheckConstraint("status IN ('open','acknowledged','resolved')", name="ck_alerts_status"),
    )


class AlertHistory(Base):
    """APPEND-ONLY. Every action taken against an alert (raised, acked, resolved)."""
    __tablename__ = "alert_history"

    id:          Mapped[str]      = mapped_column(String(36), primary_key=True, default=_uuid)
    alert_id:    Mapped[str]      = mapped_column(String(36), ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False)
    action:      Mapped[str]      = mapped_column(String(50), nullable=False)
    actor:       Mapped[str]      = mapped_column(String(100), nullable=False, default="alert_worker")
    at:          Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Dashboard(Base):
    """One row per named dashboard. layout is the widget arrangement; widgets themselves live in dashboard_widgets."""
    __tablename__ = "dashboards"

    id:          Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    name:        Mapped[str]           = mapped_column(String(150), nullable=False, unique=True)
    layout:      Mapped[Dict[str, Any]]= mapped_column(JSONB, nullable=False, default=dict)
    updated_at:  Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DashboardWidget(Base):
    """One row per widget on a dashboard."""
    __tablename__ = "dashboard_widgets"

    id:            Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    dashboard_id:  Mapped[str]           = mapped_column(String(36), ForeignKey("dashboards.id", ondelete="CASCADE"), nullable=False)
    widget_type:   Mapped[str]           = mapped_column(String(50), nullable=False)
    config:        Mapped[Dict[str, Any]]= mapped_column(JSONB, nullable=False, default=dict)
    position:      Mapped[int]           = mapped_column(Integer, nullable=False, default=0)


class MonitoringLog(Base):
    """APPEND-ONLY. Aggregated structlog output surfaced by log_analysis_worker (not a full log store — the platform's own logs stay in stdout/whatever log sink ops configures; this table holds only what Monitoring itself judged noteworthy)."""
    __tablename__ = "logs"

    id:         Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    service:    Mapped[str]           = mapped_column(String(50), nullable=False)
    level:      Mapped[str]           = mapped_column(String(20), nullable=False)
    message:    Mapped[str]           = mapped_column(Text, nullable=False)
    context:    Mapped[Dict[str, Any]]= mapped_column(JSONB, nullable=False, default=dict)
    at:         Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("level IN ('debug','info','warning','error','critical')", name="ck_logs_level"),
    )


class MonitoringTrace(Base):
    """APPEND-ONLY. OTel spans surfaced by trace_analysis_worker as latency/error hotspots (a summarized index, not a full trace store — full traces stay in the OTel backend telemetry.py already exports to)."""
    __tablename__ = "traces"

    id:           Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    trace_id:     Mapped[str]           = mapped_column(String(64), nullable=False)
    span_id:      Mapped[str]           = mapped_column(String(64), nullable=False)
    service:      Mapped[str]           = mapped_column(String(50), nullable=False)
    duration_ms:  Mapped[float]         = mapped_column(Numeric, nullable=False)
    status:       Mapped[str]           = mapped_column(String(20), nullable=False, default="ok")
    at:           Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())


class CapacityForecast(Base):
    """APPEND-ONLY. One row per cycle's trend projection for a component."""
    __tablename__ = "capacity_forecast"

    id:                  Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    component:           Mapped[str]           = mapped_column(String(50), nullable=False)
    trend_slope:         Mapped[float]         = mapped_column(Numeric, nullable=False, default=0.0)
    projected_breach_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    computed_at:         Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())


# ═══════════════════════════════════════════════════════════════
# INCIDENT RESPONSE SERVICE (M3.8) — additive only, no existing table touched.
# Platform-wide by nature, same reasoning as Monitoring's tables above:
# project_id is nullable (an incident on "postgres" or "nats" isn't
# owned by any one project); it's populated only when a recovery action
# (rollback) is actually scoped to one project's deployment.
# See docs/M3.8_Incident_Response_Handover.md.
# ═══════════════════════════════════════════════════════════════

class Incident(Base):
    """One row per incident. Updated in place as status changes
    (incident_timeline_events is the append-only trail)."""
    __tablename__ = "incidents"

    id:            Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    incident_id:   Mapped[str]           = mapped_column(String(36), nullable=False, unique=True)
    project_id:    Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("projects.id", ondelete="SET NULL"))
    component:     Mapped[str]           = mapped_column(String(50), nullable=False)
    severity:      Mapped[str]           = mapped_column(String(20), nullable=False)
    status:        Mapped[str]           = mapped_column(String(20), nullable=False, default="open")
    breach_cycles: Mapped[int]           = mapped_column(Integer, nullable=False, default=0)
    created_at:    Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at:    Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    resolved_at:   Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    closed_at:     Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("severity IN ('info','warning','critical')", name="ck_incidents_severity"),
        CheckConstraint(
            "status IN ('open','investigating','mitigating','monitoring','resolved','closed')",
            name="ck_incidents_status"),
    )


class IncidentTimelineEvent(Base):
    """APPEND-ONLY. Every lifecycle event recorded against an incident."""
    __tablename__ = "incident_timeline_events"

    id:          Mapped[str]      = mapped_column(String(36), primary_key=True, default=_uuid)
    incident_id: Mapped[str]      = mapped_column(String(36), ForeignKey("incidents.incident_id", ondelete="CASCADE"), nullable=False)
    event_type:  Mapped[str]      = mapped_column(String(50), nullable=False)
    message:     Mapped[str]      = mapped_column(Text, nullable=False)
    actor:       Mapped[str]      = mapped_column(String(100), nullable=False, default="incident_response_head")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IncidentEvidence(Base):
    """APPEND-ONLY. One row per correlated evidence item gathered by
    evidence_collection_worker."""
    __tablename__ = "incident_evidence"

    id:           Mapped[str]      = mapped_column(String(36), primary_key=True, default=_uuid)
    incident_id:  Mapped[str]      = mapped_column(String(36), ForeignKey("incidents.incident_id", ondelete="CASCADE"), nullable=False)
    source:       Mapped[str]      = mapped_column(String(30), nullable=False)
    ref:          Mapped[str]      = mapped_column(String(150), nullable=False)
    summary:      Mapped[str]      = mapped_column(Text, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("source IN ('monitoring','devops','repository')", name="ck_incident_evidence_source"),
    )


class RecoveryAction(Base):
    """One row per recovery action attempted against an incident.
    Updated in place as the action progresses (initiated -> completed/failed)."""
    __tablename__ = "recovery_actions"

    id:            Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    incident_id:   Mapped[str]           = mapped_column(String(36), ForeignKey("incidents.incident_id", ondelete="CASCADE"), nullable=False)
    project_id:    Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("projects.id", ondelete="SET NULL"))
    action_type:   Mapped[str]           = mapped_column(String(20), nullable=False)
    status:        Mapped[str]           = mapped_column(String(20), nullable=False, default="pending")
    detail:        Mapped[Dict[str, Any]]= mapped_column(JSONB, nullable=False, default=dict)
    triggered_at:  Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at:  Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("action_type IN ('rollback','restart','manual','none')", name="ck_recovery_actions_type"),
        CheckConstraint(
            "status IN ('pending','in_progress','completed','failed','skipped')",
            name="ck_recovery_actions_status"),
    )


class IncidentReportRecord(Base):
    """One row per generated incident_report artifact (final summary,
    written once at closure by reporting_worker)."""
    __tablename__ = "incident_reports"

    id:           Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    incident_id:  Mapped[str]           = mapped_column(String(36), ForeignKey("incidents.incident_id", ondelete="CASCADE"), nullable=False, unique=True)
    summary:      Mapped[str]           = mapped_column(Text, nullable=False)
    root_cause:   Mapped[Optional[str]] = mapped_column(Text)
    remediation:  Mapped[Dict[str, Any]]= mapped_column(JSONB, nullable=False, default=dict)
    generated_at: Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())


# ═══════════════════════════════════════════════════════════════
# PLATFORM INTEGRATION (M3.9) — additive only, no existing table touched.
# See docs/M3.9_Platform_Integration_Handover.md.
# ═══════════════════════════════════════════════════════════════

class PlatformReport(Base):
    """One row per generated platform readiness report
    (services.integration.orchestrator.generate_full_report)."""
    __tablename__ = "platform_reports"

    id:                   Mapped[str]      = mapped_column(String(36), primary_key=True, default=_uuid)
    readiness_overall:    Mapped[float]    = mapped_column(Numeric, nullable=False, default=0.0)
    readiness_categories: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    health_overall:       Mapped[str]      = mapped_column(String(20), nullable=False)
    summary:              Mapped[str]      = mapped_column(Text, nullable=False, default="")
    generated_at:         Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("health_overall IN ('healthy','degraded','failed')", name="ck_platform_reports_health"),
    )


class ValidationResult(Base):
    """APPEND-ONLY. One row per validator category run as part of a
    platform report (registry/workflows/events/artifacts/health)."""
    __tablename__ = "validation_results"

    id:         Mapped[str]      = mapped_column(String(36), primary_key=True, default=_uuid)
    report_id:  Mapped[str]      = mapped_column(String(36), ForeignKey("platform_reports.id", ondelete="CASCADE"), nullable=False)
    category:   Mapped[str]      = mapped_column(String(50), nullable=False)
    passed:     Mapped[bool]     = mapped_column(Boolean, nullable=False, default=False)
    detail:     Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DependencyCheck(Base):
    """APPEND-ONLY. One row per department dependency check performed
    as part of a platform report (services.integration.dependency_graph)."""
    __tablename__ = "dependency_checks"

    id:         Mapped[str]      = mapped_column(String(36), primary_key=True, default=_uuid)
    report_id:  Mapped[str]      = mapped_column(String(36), ForeignKey("platform_reports.id", ondelete="CASCADE"), nullable=False)
    department: Mapped[str]      = mapped_column(String(50), nullable=False)
    passed:     Mapped[bool]     = mapped_column(Boolean, nullable=False, default=False)
    missing:    Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
