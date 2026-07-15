"""
services/incident_response/api/routes.py
=================================
Incident Response Service's HTTP surface:
    GET  /incident-response/incidents
    GET  /incident-response/incidents/{incident_id}
    GET  /incident-response/incidents/{incident_id}/timeline
    POST /incident-response/incidents/manual
    POST /incident-response/incidents/close

Reads are DB-backed (incidents/incident_timeline_events, mirrors
Monitoring's own DB-backed-reads decision, spec §0 Decision 4's
precedent) rather than served from an in-memory registry.

POST /incidents/manual is an operator escape hatch — mirrors DevOps's
own `POST /devops/rollback` (services/devops/api/routes.py) — for
opening an incident Incident Response itself decided not to (or
Monitoring hasn't yet detected). It writes the Incident row directly;
it does NOT run the full W-INCIDENT-RESPONSE graph (that always goes
through the `monitoring.incident` NATS event, api/events.py), so a
manually-opened incident is investigated/closed by a human via
POST /incidents/close, not auto-classified.
"""
from __future__ import annotations

from typing import Any, Dict, List

import structlog
from fastapi import APIRouter, Depends, HTTPException

from infrastructure.database.connection import get_db
from services.incident_response.integration.incident_repository import (
    IncidentRepository, IncidentTimelineRepository,
)
from services.incident_response.schemas import (
    CloseIncidentRequest, IncidentResponse, IncidentTimelineResponse, ManualIncidentRequest,
)

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/incident-response", tags=["Incident Response"])


@router.get("/incidents", response_model=List[IncidentResponse])
async def list_incidents(db=Depends(get_db)) -> List[IncidentResponse]:
    rows = await IncidentRepository.list_open(db)
    return [
        IncidentResponse(
            incident_id=r.incident_id, component=r.component, severity=r.severity,
            status=r.status, breach_cycles=r.breach_cycles, created_at=r.created_at,
            updated_at=r.updated_at, resolved_at=r.resolved_at, closed_at=r.closed_at,
        )
        for r in rows
    ]


@router.get("/incidents/{incident_id}", response_model=IncidentResponse)
async def get_incident(incident_id: str, db=Depends(get_db)) -> IncidentResponse:
    row = await IncidentRepository.get(db, incident_id)
    if row is None:
        raise HTTPException(404, f"No incident {incident_id!r}")
    return IncidentResponse(
        incident_id=row.incident_id, component=row.component, severity=row.severity,
        status=row.status, breach_cycles=row.breach_cycles, created_at=row.created_at,
        updated_at=row.updated_at, resolved_at=row.resolved_at, closed_at=row.closed_at,
    )


@router.get("/incidents/{incident_id}/timeline", response_model=IncidentTimelineResponse)
async def get_incident_timeline(incident_id: str, db=Depends(get_db)) -> IncidentTimelineResponse:
    rows = await IncidentTimelineRepository.list_for(db, incident_id)
    return IncidentTimelineResponse(incident_id=incident_id, entries=[
        {"event_type": r.event_type, "message": r.message, "actor": r.actor,
         "occurred_at": r.occurred_at.isoformat()}
        for r in rows
    ])


@router.post("/incidents/manual")
async def open_incident_manually(req: ManualIncidentRequest, db=Depends(get_db)) -> Dict[str, Any]:
    import uuid
    incident_id = str(uuid.uuid4())
    await IncidentRepository.get_or_create(
        db, incident_id, req.component.value, req.severity.value, req.breach_cycles)
    await IncidentTimelineRepository.record(
        db, incident_id, "incident_opened", f"Manually opened: {req.reason}", actor="operator")
    return {"incident_id": incident_id, "status": "open"}


@router.post("/incidents/close")
async def close_incident(req: CloseIncidentRequest, db=Depends(get_db)) -> Dict[str, Any]:
    row = await IncidentRepository.get(db, req.incident_id)
    if row is None:
        raise HTTPException(404, f"No incident {req.incident_id!r}")
    await IncidentRepository.update_status(db, req.incident_id, "closed")
    await IncidentTimelineRepository.record(
        db, req.incident_id, "incident_closed",
        f"Closed by {req.closed_by or 'operator'}.", actor=req.closed_by or "operator")
    return {"incident_id": req.incident_id, "status": "closed"}
