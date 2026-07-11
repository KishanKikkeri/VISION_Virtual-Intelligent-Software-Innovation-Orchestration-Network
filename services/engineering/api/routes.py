"""
services/engineering/api/routes.py
=====================================
Engineering Service's HTTP surface. Thin by design: Engineering has no
ORM tables of its own (see models/__init__.py docstring), so status is
served from an in-memory registry updated by EngineeringHead as it runs,
plus the generic Artifact table for durable history.

Mountable standalone (services/engineering/main.py) or into the
Manager Service's app — both are supported.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import structlog
from fastapi import APIRouter, HTTPException

from services.engineering.schemas import (
    EngineeringStatusResponse,
    StartEngineeringRequest,
)

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/engineering", tags=["Engineering"])

# In-memory status registry: project_id -> EngineeringStatusResponse-shaped dict.
# Populated by EngineeringHead / the graph runner; not a source of truth for
# audit history (that's the Artifact + AuditEvent tables, same as every
# other department).
_STATUS: Dict[str, Dict[str, Any]] = {}


def record_status(project_id: str, status: Dict[str, Any]) -> None:
    """Called by EngineeringHead / graph nodes to update the in-memory registry."""
    _STATUS[project_id] = status


def get_status(project_id: str) -> Optional[Dict[str, Any]]:
    return _STATUS.get(project_id)


@router.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok", "service": "engineering"}


@router.get("/status/{project_id}", response_model=EngineeringStatusResponse)
async def get_engineering_status(project_id: str) -> EngineeringStatusResponse:
    status = get_status(project_id)
    if status is None:
        raise HTTPException(404, f"No engineering run found for project {project_id}")
    return EngineeringStatusResponse(**status)


@router.post("/start")
async def start_engineering(req: StartEngineeringRequest) -> Dict[str, Any]:
    """
    Manual/ops trigger. In normal operation the Manager Service starts
    Engineering automatically on `architecture.design.completed`
    (see api/events.py) — this endpoint exists for re-runs and testing.
    """
    record_status(req.project_id, {
        "project_id": req.project_id,
        "phase_status": "running",
        "plan": None,
        "modules_generated": 0,
        "review_cycles_run": 0,
        "build_result": None,
    })
    return {"status": "started", "project_id": req.project_id, "feature_name": req.feature_name}
