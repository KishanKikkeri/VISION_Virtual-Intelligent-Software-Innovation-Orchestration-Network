"""
services/incident_response/integration/incident_repository.py
=========================================================
Repository-pattern wrapper around the M3.8 ORM tables (Incident,
IncidentTimelineEvent, IncidentEvidence, RecoveryAction,
IncidentReportRecord — see infrastructure/database/models.py). Kept
inside services/incident_response/ rather than the shared
infrastructure/database/repositories/__init__.py, mirroring
Monitoring's self-contained `monitoring_repository.py` precedent.

All methods are static and take an AsyncSession as their first
argument, matching the existing ArtifactRepository/MonitoringRepository
convention.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select, update

from infrastructure.database.models import (
    Incident, IncidentEvidence, IncidentReportRecord,
    IncidentTimelineEvent, RecoveryAction,
)


class IncidentRepository:
    @staticmethod
    async def get_or_create(db, incident_id: str, component: str, severity: str,
                             breach_cycles: int, project_id: Optional[str] = None) -> Incident:
        r = await db.execute(select(Incident).where(Incident.incident_id == incident_id))
        existing = r.scalar_one_or_none()
        if existing:
            return existing
        row = Incident(incident_id=incident_id, component=component, severity=severity,
                        status="open", breach_cycles=breach_cycles, project_id=project_id)
        db.add(row)
        await db.flush()
        return row

    @staticmethod
    async def update_status(db, incident_id: str, status: str) -> None:
        values: Dict[str, Any] = {"status": status, "updated_at": datetime.utcnow()}
        if status == "resolved":
            values["resolved_at"] = datetime.utcnow()
        if status == "closed":
            values["closed_at"] = datetime.utcnow()
        await db.execute(update(Incident).where(Incident.incident_id == incident_id).values(**values))

    @staticmethod
    async def get(db, incident_id: str) -> Optional[Incident]:
        r = await db.execute(select(Incident).where(Incident.incident_id == incident_id))
        return r.scalar_one_or_none()

    @staticmethod
    async def list_open(db) -> List[Incident]:
        r = await db.execute(select(Incident).where(Incident.status != "closed")
                              .order_by(Incident.updated_at.desc()))
        return list(r.scalars().all())


class IncidentTimelineRepository:
    @staticmethod
    async def record(db, incident_id: str, event_type: str, message: str,
                      actor: str = "incident_response_head") -> IncidentTimelineEvent:
        row = IncidentTimelineEvent(incident_id=incident_id, event_type=event_type,
                                     message=message, actor=actor)
        db.add(row)
        await db.flush()
        return row

    @staticmethod
    async def list_for(db, incident_id: str) -> List[IncidentTimelineEvent]:
        r = await db.execute(
            select(IncidentTimelineEvent).where(IncidentTimelineEvent.incident_id == incident_id)
            .order_by(IncidentTimelineEvent.occurred_at.asc()))
        return list(r.scalars().all())


class IncidentEvidenceRepository:
    @staticmethod
    async def record(db, incident_id: str, source: str, ref: str, summary: str) -> IncidentEvidence:
        row = IncidentEvidence(incident_id=incident_id, source=source, ref=ref, summary=summary)
        db.add(row)
        await db.flush()
        return row

    @staticmethod
    async def list_for(db, incident_id: str) -> List[IncidentEvidence]:
        r = await db.execute(
            select(IncidentEvidence).where(IncidentEvidence.incident_id == incident_id)
            .order_by(IncidentEvidence.collected_at.asc()))
        return list(r.scalars().all())


class RecoveryActionRepository:
    @staticmethod
    async def create(db, incident_id: str, action_type: str,
                      project_id: Optional[str] = None) -> RecoveryAction:
        row = RecoveryAction(incident_id=incident_id, action_type=action_type,
                              status="pending", project_id=project_id)
        db.add(row)
        await db.flush()
        return row

    @staticmethod
    async def update_status(db, action_id: str, status: str,
                             detail: Optional[Dict[str, Any]] = None) -> None:
        values: Dict[str, Any] = {"status": status}
        if detail is not None:
            values["detail"] = detail
        if status == "in_progress":
            values["triggered_at"] = datetime.utcnow()
        if status in ("completed", "failed"):
            values["completed_at"] = datetime.utcnow()
        await db.execute(update(RecoveryAction).where(RecoveryAction.id == action_id).values(**values))

    @staticmethod
    async def latest_for(db, incident_id: str) -> Optional[RecoveryAction]:
        r = await db.execute(
            select(RecoveryAction).where(RecoveryAction.incident_id == incident_id)
            .order_by(RecoveryAction.id.desc()).limit(1))
        return r.scalar_one_or_none()


class IncidentReportRepository:
    @staticmethod
    async def record(db, incident_id: str, summary: str, root_cause: Optional[str],
                      remediation: Dict[str, Any]) -> IncidentReportRecord:
        r = await db.execute(select(IncidentReportRecord).where(IncidentReportRecord.incident_id == incident_id))
        existing = r.scalar_one_or_none()
        if existing:
            existing.summary = summary
            existing.root_cause = root_cause
            existing.remediation = remediation
            existing.generated_at = datetime.utcnow()
            await db.flush()
            return existing
        row = IncidentReportRecord(incident_id=incident_id, summary=summary,
                                    root_cause=root_cause, remediation=remediation)
        db.add(row)
        await db.flush()
        return row
