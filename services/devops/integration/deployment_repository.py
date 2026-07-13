"""
services/devops/integration/deployment_repository.py
=========================================================
Repository-pattern wrapper around the M3.6 ORM tables (Deployment,
DeploymentHistory, DeploymentHealth, ReleaseMetadata, RollbackRecord —
see infrastructure/database/models.py). Kept inside services/devops/
(rather than added to the shared infrastructure/database/repositories/
__init__.py) so DevOps owns its own persistence boundary end to end,
the same self-contained-integration pattern Security used for its own
repository_client.py rather than importing QA's.

All methods are static and take an AsyncSession as their first
argument, matching the existing ArtifactRepository/RepositoryRepository
convention in infrastructure/database/repositories/__init__.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from infrastructure.database.models import (
    Deployment, DeploymentHealth, DeploymentHistory,
    ReleaseMetadata, RollbackRecord,
)


class DeploymentRepository:
    @staticmethod
    async def create(db, project_id: str, workflow_id: Optional[str] = None,
                      environment: str = "production", version: Optional[str] = None,
                      deployment_plan_ref: Optional[str] = None,
                      triggered_by: str = "manager_agent") -> Deployment:
        d = Deployment(project_id=project_id, workflow_id=workflow_id, environment=environment,
                        version=version, status="pending", deployment_plan_ref=deployment_plan_ref,
                        triggered_by=triggered_by)
        db.add(d)
        await db.flush()
        return d

    @staticmethod
    async def update_status(db, deployment_id: str, status: str,
                             failure_reason: Optional[str] = None,
                             approved_by: Optional[str] = None) -> None:
        from sqlalchemy import update
        vals: Dict[str, Any] = {"status": status}
        if failure_reason is not None:
            vals["failure_reason"] = failure_reason
        if approved_by is not None:
            vals["approved_by"] = approved_by
        if status == "deploying":
            vals["started_at"] = datetime.utcnow()
        if status in ("healthy", "failed", "rolled_back"):
            vals["completed_at"] = datetime.utcnow()
        await db.execute(update(Deployment).where(Deployment.id == deployment_id).values(**vals))

    @staticmethod
    async def get_by_id(db, deployment_id: str) -> Optional[Deployment]:
        r = await db.execute(select(Deployment).where(Deployment.id == deployment_id))
        return r.scalar_one_or_none()

    @staticmethod
    async def get_latest_for_project(db, project_id: str) -> Optional[Deployment]:
        r = await db.execute(
            select(Deployment).where(Deployment.project_id == project_id)
            .order_by(Deployment.created_at.desc()).limit(1))
        return r.scalar_one_or_none()


class DeploymentHistoryRepository:
    @staticmethod
    async def record(db, deployment_id: str, project_id: str, event_type: str,
                      status: str, payload: Optional[Dict[str, Any]] = None) -> DeploymentHistory:
        h = DeploymentHistory(deployment_id=deployment_id, project_id=project_id,
                               event_type=event_type, status=status, payload=payload or {})
        db.add(h)
        await db.flush()
        return h

    @staticmethod
    async def list_for_deployment(db, deployment_id: str) -> List[DeploymentHistory]:
        r = await db.execute(
            select(DeploymentHistory).where(DeploymentHistory.deployment_id == deployment_id)
            .order_by(DeploymentHistory.recorded_at.asc()))
        return list(r.scalars().all())


class DeploymentHealthRepository:
    @staticmethod
    async def record_checks(db, deployment_id: str, project_id: str,
                             checks: List[Dict[str, Any]]) -> List[DeploymentHealth]:
        rows = []
        for c in checks:
            row = DeploymentHealth(deployment_id=deployment_id, project_id=project_id,
                                    check_name=c["check_name"], passed=bool(c["passed"]),
                                    detail=c.get("detail"))
            db.add(row)
            rows.append(row)
        await db.flush()
        return rows

    @staticmethod
    async def list_for_deployment(db, deployment_id: str) -> List[DeploymentHealth]:
        r = await db.execute(
            select(DeploymentHealth).where(DeploymentHealth.deployment_id == deployment_id)
            .order_by(DeploymentHealth.checked_at.asc()))
        return list(r.scalars().all())


class ReleaseMetadataRepository:
    @staticmethod
    async def create(db, project_id: str, version: str, deployment_id: Optional[str] = None,
                      previous_version: Optional[str] = None,
                      release_notes: Optional[str] = None) -> ReleaseMetadata:
        rel = ReleaseMetadata(project_id=project_id, deployment_id=deployment_id, version=version,
                               previous_version=previous_version, release_notes=release_notes)
        db.add(rel)
        await db.flush()
        return rel

    @staticmethod
    async def get_latest_for_project(db, project_id: str) -> Optional[ReleaseMetadata]:
        r = await db.execute(
            select(ReleaseMetadata).where(ReleaseMetadata.project_id == project_id)
            .order_by(ReleaseMetadata.released_at.desc()).limit(1))
        return r.scalar_one_or_none()


class RollbackRecordRepository:
    @staticmethod
    async def create(db, deployment_id: str, project_id: str, reason: str,
                      rolled_back_to_version: Optional[str] = None,
                      status: str = "initiated") -> RollbackRecord:
        rb = RollbackRecord(deployment_id=deployment_id, project_id=project_id, reason=reason,
                             rolled_back_to_version=rolled_back_to_version, status=status)
        db.add(rb)
        await db.flush()
        return rb

    @staticmethod
    async def mark_completed(db, rollback_id: str, status: str = "completed") -> None:
        from sqlalchemy import update
        await db.execute(update(RollbackRecord).where(RollbackRecord.id == rollback_id)
                          .values(status=status, completed_at=datetime.utcnow()))
