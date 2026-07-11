"""
services/qa/api/routes.py
============================
QA Service's HTTP surface. Thin by design: QA has no ORM tables of its
own (see models/__init__.py docstring), so status is served from an
in-memory registry updated by QAHead as it runs, plus the generic
Artifact table for durable history. Mirrors services/engineering/api/routes.py.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import structlog
from fastapi import APIRouter, HTTPException

from services.qa.schemas import QAStatusResponse, StartQARequest

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/qa", tags=["QA"])

_STATUS: Dict[str, Dict[str, Any]] = {}


def record_status(project_id: str, status: Dict[str, Any]) -> None:
    """Called by QAHead / graph nodes to update the in-memory registry."""
    _STATUS[project_id] = status


def get_status(project_id: str) -> Optional[Dict[str, Any]]:
    return _STATUS.get(project_id)


@router.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok", "service": "qa"}


@router.get("/status/{project_id}", response_model=QAStatusResponse)
async def get_qa_status(project_id: str) -> QAStatusResponse:
    status = get_status(project_id)
    if status is None:
        raise HTTPException(404, f"No QA run found for project {project_id}")
    return QAStatusResponse(**status)


@router.post("/start")
async def start_qa(req: StartQARequest) -> Dict[str, Any]:
    """
    Manual/ops trigger. In normal operation the Manager Service starts
    QA automatically on `engineering.phase.completed` (see api/events.py)
    — this endpoint exists for re-runs and testing.
    """
    record_status(req.project_id, {
        "project_id": req.project_id,
        "phase_status": "running",
        "plan": None,
        "suites_generated": 0,
        "coverage_pct": 0.0,
        "qa_report": None,
        "defects": [],
    })
    return {"status": "started", "project_id": req.project_id, "feature_name": req.feature_name}
