"""
infrastructure/database/repositories/__init__.py
Repository pattern — one class per aggregate root.
"""
from __future__ import annotations
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from infrastructure.database.models import (
    Agent, AgentRun, Approval, Artifact, AuditEvent,
    Branch, BudgetLimit, Project, PullRequest, Repository, RepositoryEvent,
    TokenLedger, User, Workflow, WorkflowPhase,
)

class ProjectRepository:
    @staticmethod
    async def create(db, name, description, owner_id, llm_provider="anthropic", budget_usd=None):
        pid = str(uuid.uuid4())
        project = Project(id=pid, name=name, description=description,
                          owner_id=owner_id, llm_provider=llm_provider,
                          status="initializing", current_phase=1)
        db.add(project)
        await db.flush()
        wf = Workflow(project_id=pid, current_phase=1, status="active")
        db.add(wf)
        await db.flush()
        phase_names = ["Idea Collection","Requirements Analysis","Architecture Design",
                       "Project Structure","Implementation","Testing","Security Review",
                       "Deployment","Production Monitoring","Continuous Improvement"]
        for i, n in enumerate(phase_names, 1):
            db.add(WorkflowPhase(workflow_id=wf.id, project_id=pid,
                                 phase_number=i, phase_name=n,
                                 status="pending", requires_approval=(i in {2,3,8})))
        db.add(BudgetLimit(project_id=pid, limit_usd=budget_usd,
                           status="active" if budget_usd else "unlimited"))
        return project

    @staticmethod
    async def get_by_id(db, project_id):
        r = await db.execute(select(Project).where(Project.id == project_id))
        return r.scalar_one_or_none()

    @staticmethod
    async def list_by_owner(db, owner_id):
        r = await db.execute(select(Project).where(Project.owner_id == owner_id)
                             .order_by(Project.created_at.desc()))
        return list(r.scalars().all())

    @staticmethod
    async def update_status(db, project_id, status, phase=None):
        vals = {"status": status, "updated_at": datetime.utcnow()}
        if phase: vals["current_phase"] = phase
        await db.execute(update(Project).where(Project.id == project_id).values(**vals))

    @staticmethod
    async def get_total_spend(db, project_id):
        r = await db.execute(select(func.coalesce(func.sum(TokenLedger.cost_usd), 0))
                             .where(TokenLedger.project_id == project_id))
        return float(r.scalar_one())


class ArtifactRepository:
    @staticmethod
    async def create(db, project_id, artifact_type, created_by,
                     content=None, storage_ref=None, metadata=None):
        r = await db.execute(
            select(func.coalesce(func.max(Artifact.version), 0))
            .where(Artifact.project_id == project_id,
                   Artifact.artifact_type == artifact_type))
        version = int(r.scalar_one()) + 1
        a = Artifact(project_id=project_id, artifact_type=artifact_type,
                     version=version, created_by=created_by, status="draft",
                     content=content if isinstance(content, dict) else None,
                     storage_ref=storage_ref, metadata_=metadata or {})
        db.add(a)
        await db.flush()
        return {"artifact_id": a.id, "artifact_type": a.artifact_type,
                "version": a.version, "storage_ref": a.storage_ref}

    @staticmethod
    async def get_latest_approved(db, project_id, artifact_type):
        r = await db.execute(
            select(Artifact)
            .where(Artifact.project_id == project_id,
                   Artifact.artifact_type == artifact_type,
                   Artifact.status == "approved")
            .order_by(Artifact.version.desc()).limit(1))
        return r.scalar_one_or_none()

    @staticmethod
    async def list_for_project(db, project_id):
        r = await db.execute(
            select(Artifact).where(Artifact.project_id == project_id)
            .order_by(Artifact.artifact_type, Artifact.version.desc()))
        return list(r.scalars().all())

    @staticmethod
    async def update_status(db, artifact_id, status, approved_by=None):
        vals = {"status": status, "updated_at": datetime.utcnow()}
        if approved_by: vals["approved_by"] = approved_by
        await db.execute(update(Artifact).where(Artifact.id == artifact_id).values(**vals))

    @staticmethod
    async def list_versions_for_type(db, project_id, artifact_type):
        """M4.2: every version of one artifact_type for a project, oldest
        first — the sequence services.integration.replay.artifact_diff
        compares two points along."""
        r = await db.execute(
            select(Artifact).where(Artifact.project_id == project_id,
                                    Artifact.artifact_type == artifact_type)
            .order_by(Artifact.version.asc()))
        return list(r.scalars().all())

    @staticmethod
    async def get_version(db, project_id, artifact_type, version):
        r = await db.execute(
            select(Artifact).where(Artifact.project_id == project_id,
                                    Artifact.artifact_type == artifact_type,
                                    Artifact.version == version))
        return r.scalar_one_or_none()


class AuditRepository:
    @staticmethod
    async def record(db, project_id=None, event_type="", actor_type="system",
                     actor_id="system", entity_type=None, entity_id=None, payload=None):
        row = AuditEvent(project_id=project_id, event_type=event_type,
                         actor_type=actor_type, actor_id=actor_id,
                         entity_type=entity_type, entity_id=entity_id,
                         payload=payload or {})
        db.add(row)
        await db.flush()
        return row.id

    @staticmethod
    async def list_for_project(db, project_id, limit=100):
        r = await db.execute(
            select(AuditEvent).where(AuditEvent.project_id == project_id)
            .order_by(AuditEvent.recorded_at.desc()).limit(limit))
        return list(r.scalars().all())

    @staticmethod
    async def list_for_project_ascending(db, project_id, limit=1000):
        """M4.2: oldest-first ordering for execution_timeline/replay_engine,
        which need to walk a project's history forward in time. Kept
        separate from list_for_project (used by /platform/traces, which
        wants most-recent-first) rather than adding an order_by param
        that would change that endpoint's existing, relied-upon order."""
        r = await db.execute(
            select(AuditEvent).where(AuditEvent.project_id == project_id)
            .order_by(AuditEvent.recorded_at.asc()).limit(limit))
        return list(r.scalars().all())

    @staticmethod
    async def list_recent(db, limit=200, event_type_prefix=None):
        """M4.3: platform-wide (not one project's) most-recent-first
        event read, for the Live Operations Dashboard's Event Stream
        card. Every other AuditRepository method here is scoped to a
        single project_id; the dashboard is the first caller that
        genuinely needs "what happened anywhere on the platform,
        recently" rather than "what happened on project X" — so this
        is a new, additive method rather than a change to any existing
        one's filtering behavior. `event_type_prefix` gives an
        index-friendly category filter (dashboard_builder.
        categorize_event_type's convention: the first dot-segment of
        event_type) without loading every row and filtering in Python."""
        stmt = select(AuditEvent).order_by(AuditEvent.recorded_at.desc()).limit(limit)
        if event_type_prefix:
            stmt = select(AuditEvent).where(
                AuditEvent.event_type.like(f"{event_type_prefix}.%") | (AuditEvent.event_type == event_type_prefix)
            ).order_by(AuditEvent.recorded_at.desc()).limit(limit)
        r = await db.execute(stmt)
        return list(r.scalars().all())


class TokenLedgerRepository:
    @staticmethod
    async def record(db, project_id, agent_id, department, provider, model,
                     input_tokens, output_tokens, cost_usd, agent_run_id=None):
        row = TokenLedger(project_id=project_id, agent_run_id=agent_run_id,
                          agent_id=agent_id, department=department,
                          provider=provider, model=model,
                          input_tokens=input_tokens, output_tokens=output_tokens,
                          cost_usd=cost_usd)
        db.add(row)
        await db.flush()
        return row.id

    @staticmethod
    async def get_project_spend_by_dept(db, project_id):
        r = await db.execute(
            select(TokenLedger.department, func.sum(TokenLedger.cost_usd))
            .where(TokenLedger.project_id == project_id)
            .group_by(TokenLedger.department))
        return {d: float(c) for d, c in r.all()}


class UserRepository:
    @staticmethod
    async def create(db, email, password_hash, full_name=None, role="developer"):
        u = User(email=email, password_hash=password_hash,
                 full_name=full_name, role=role)
        db.add(u)
        await db.flush()
        return u

    @staticmethod
    async def get_by_email(db, email):
        r = await db.execute(select(User).where(User.email == email))
        return r.scalar_one_or_none()

    @staticmethod
    async def get_by_id(db, user_id):
        r = await db.execute(select(User).where(User.id == user_id))
        return r.scalar_one_or_none()


# ═══════════════════════════════════════════════════════════════
# M3.2 — REPOSITORY SERVICE REPOSITORIES
# ═══════════════════════════════════════════════════════════════

class RepositoryRepository:
    @staticmethod
    async def create(db, project_id, provider, owner, name, full_name,
                     default_branch="main", clone_url=None, html_url=None,
                     visibility="private", provider_repo_id=None, metadata=None):
        row = Repository(project_id=project_id, provider=provider, owner=owner,
                         name=name, full_name=full_name, default_branch=default_branch,
                         clone_url=clone_url, html_url=html_url, visibility=visibility,
                         status="active", provider_repo_id=provider_repo_id,
                         metadata_=metadata or {})
        db.add(row)
        await db.flush()
        return row

    @staticmethod
    async def get_by_id(db, repository_id):
        r = await db.execute(select(Repository).where(Repository.id == repository_id))
        return r.scalar_one_or_none()

    @staticmethod
    async def get_by_project(db, project_id):
        r = await db.execute(select(Repository).where(Repository.project_id == project_id))
        return r.scalar_one_or_none()

    @staticmethod
    async def update_status(db, repository_id, status):
        await db.execute(update(Repository).where(Repository.id == repository_id)
                         .values(status=status, updated_at=datetime.utcnow()))


class BranchRepository:
    @staticmethod
    async def create(db, repository_id, name, branch_type="feature", task_id=None,
                     base_branch="develop", head_sha=None, is_protected=False,
                     created_by="VISION Bot"):
        row = Branch(repository_id=repository_id, name=name, branch_type=branch_type,
                     task_id=task_id, base_branch=base_branch, head_sha=head_sha,
                     is_protected=is_protected, status="active", created_by=created_by)
        db.add(row)
        await db.flush()
        return row

    @staticmethod
    async def get_by_name(db, repository_id, name):
        r = await db.execute(select(Branch).where(Branch.repository_id == repository_id,
                                                   Branch.name == name))
        return r.scalar_one_or_none()

    @staticmethod
    async def list_for_repository(db, repository_id, status=None):
        stmt = select(Branch).where(Branch.repository_id == repository_id)
        if status:
            stmt = stmt.where(Branch.status == status)
        r = await db.execute(stmt.order_by(Branch.created_at.desc()))
        return list(r.scalars().all())

    @staticmethod
    async def update_head(db, branch_id, head_sha):
        await db.execute(update(Branch).where(Branch.id == branch_id).values(head_sha=head_sha))

    @staticmethod
    async def mark_merged(db, branch_id):
        await db.execute(update(Branch).where(Branch.id == branch_id)
                         .values(status="merged", merged_at=datetime.utcnow()))

    @staticmethod
    async def mark_deleted(db, branch_id):
        await db.execute(update(Branch).where(Branch.id == branch_id)
                         .values(status="deleted", deleted_at=datetime.utcnow()))


class PullRequestRepository:
    @staticmethod
    async def create(db, repository_id, title, source_branch, target_branch="develop",
                     description=None, task_id=None, provider_pr_number=None,
                     reviewers=None, html_url=None, merge_strategy="squash"):
        row = PullRequest(repository_id=repository_id, title=title,
                          source_branch=source_branch, target_branch=target_branch,
                          description=description, task_id=task_id,
                          provider_pr_number=provider_pr_number,
                          reviewers=reviewers or [], html_url=html_url,
                          merge_strategy=merge_strategy, status="open")
        db.add(row)
        await db.flush()
        return row

    @staticmethod
    async def get_by_id(db, pull_request_id):
        r = await db.execute(select(PullRequest).where(PullRequest.id == pull_request_id))
        return r.scalar_one_or_none()

    @staticmethod
    async def list_for_repository(db, repository_id, status=None):
        stmt = select(PullRequest).where(PullRequest.repository_id == repository_id)
        if status:
            stmt = stmt.where(PullRequest.status == status)
        r = await db.execute(stmt.order_by(PullRequest.opened_at.desc()))
        return list(r.scalars().all())

    @staticmethod
    async def mark_approved(db, pull_request_id):
        await db.execute(update(PullRequest).where(PullRequest.id == pull_request_id)
                         .values(status="approved", approved_at=datetime.utcnow()))

    @staticmethod
    async def mark_merged(db, pull_request_id, merge_sha):
        await db.execute(update(PullRequest).where(PullRequest.id == pull_request_id)
                         .values(status="merged", merge_sha=merge_sha, merged_at=datetime.utcnow()))

    @staticmethod
    async def mark_closed(db, pull_request_id):
        await db.execute(update(PullRequest).where(PullRequest.id == pull_request_id)
                         .values(status="closed", closed_at=datetime.utcnow()))

    @staticmethod
    async def mark_conflicted(db, pull_request_id):
        await db.execute(update(PullRequest).where(PullRequest.id == pull_request_id)
                         .values(status="conflicted"))


class RepositoryEventRepository:
    """APPEND-ONLY. No UPDATE or DELETE permitted."""

    @staticmethod
    async def record(db, event_type, repository_id=None, project_id=None,
                     entity_type=None, entity_id=None, actor="VISION Bot", payload=None):
        row = RepositoryEvent(repository_id=repository_id, project_id=project_id,
                              event_type=event_type, entity_type=entity_type,
                              entity_id=entity_id, actor=actor, payload=payload or {})
        db.add(row)
        await db.flush()
        return row.id

    @staticmethod
    async def list_for_repository(db, repository_id, limit=100):
        r = await db.execute(
            select(RepositoryEvent).where(RepositoryEvent.repository_id == repository_id)
            .order_by(RepositoryEvent.recorded_at.desc()).limit(limit))
        return list(r.scalars().all())
