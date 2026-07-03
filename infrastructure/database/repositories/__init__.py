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
    BudgetLimit, Project, TokenLedger, User, Workflow, WorkflowPhase,
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
