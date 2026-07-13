"""
services/security/api/routes.py
===================================
Security Service's HTTP surface. Thin by design: Security has no ORM
tables of its own (see models/__init__.py docstring), so status is
served from an in-memory registry updated by SecurityHead as it runs,
plus the generic Artifact table for durable history. Mirrors
services/qa/api/routes.py.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import structlog
from fastapi import APIRouter, HTTPException

from services.security.schemas import SecurityStatusResponse, StartSecurityRequest

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/security", tags=["Security"])

_STATUS: Dict[str, Dict[str, Any]] = {}


def record_status(project_id: str, status: Dict[str, Any]) -> None:
    """Called by SecurityHead / graph nodes to update the in-memory registry."""
    _STATUS[project_id] = status


def get_status(project_id: str) -> Optional[Dict[str, Any]]:
    return _STATUS.get(project_id)


@router.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok", "service": "security"}


@router.get("/status/{project_id}", response_model=SecurityStatusResponse)
async def get_security_status(project_id: str) -> SecurityStatusResponse:
    status = get_status(project_id)
    if status is None:
        raise HTTPException(404, f"No Security run found for project {project_id}")
    return SecurityStatusResponse(**status)


@router.post("/start")
async def start_security(req: StartSecurityRequest) -> Dict[str, Any]:
    """
    Manual/ops trigger. In normal operation the Manager Service starts
    Security automatically on `engineering.phase.completed` (see
    api/events.py), in parallel with QA — this endpoint exists for
    re-runs and testing.
    """
    record_status(req.project_id, {
        "project_id": req.project_id,
        "phase_status": "running",
        "plan": None,
        "scans_completed": 0,
        "risk_score": 0.0,
        "security_report": None,
        "risk_assessment": None,
        "findings": [],
    })
    return {"status": "started", "project_id": req.project_id, "feature_name": req.feature_name}
