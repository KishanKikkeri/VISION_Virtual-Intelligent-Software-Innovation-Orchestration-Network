"""
services/devops/api/routes.py
=================================
DevOps Service's HTTP surface, per the spec's API section:
    POST /deploy   POST /rollback   GET /deployments/{project_id}
    GET /health    POST /approve

Status is served from an in-memory registry updated as DevOpsHead runs,
plus the durable ORM tables (Deployment/DeploymentHistory/...) for real
history. Mirrors services/qa/api/routes.py and
services/security/api/routes.py.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import structlog
from fastapi import APIRouter, HTTPException

from services.devops.schemas import (
    ApproveDeploymentRequest,
    DeploymentStatusResponse,
    DeployRequest,
    RollbackRequest,
)

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/devops", tags=["DevOps"])

_STATUS: Dict[str, Dict[str, Any]] = {}


def record_status(project_id: str, status: Dict[str, Any]) -> None:
    """Called by DevOpsHead / graph nodes to update the in-memory registry."""
    _STATUS[project_id] = status


def get_status(project_id: str) -> Optional[Dict[str, Any]]:
    return _STATUS.get(project_id)


@router.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok", "service": "devops"}


@router.get("/deployments/{project_id}", response_model=DeploymentStatusResponse)
async def get_deployment_status(project_id: str) -> DeploymentStatusResponse:
    status = get_status(project_id)
    if status is None:
        raise HTTPException(404, f"No DevOps run found for project {project_id}")
    return DeploymentStatusResponse(**status)


@router.post("/deploy")
async def start_deploy(req: DeployRequest) -> Dict[str, Any]:
    """
    Manual/ops trigger for Stage A (generate deployment plan). In
    normal operation Manager starts this automatically once BOTH
    qa.phase.completed and security.phase.completed have fired (see
    api/events.py) — this endpoint exists for re-runs and testing.
    """
    record_status(req.project_id, {
        "project_id": req.project_id, "phase_status": "running", "status": "pending",
    })
    return {"status": "started", "project_id": req.project_id, "feature_name": req.feature_name}


@router.post("/approve")
async def approve_deployment(req: ApproveDeploymentRequest) -> Dict[str, Any]:
    """
    Standalone-mode equivalent of Manager's `POST /projects/{id}/approve`
    for the deployment_plan artifact type. In the platform's normal
    in-process runtime, Manager's own endpoint is authoritative (see
    services/manager/main.py's `deployment_plan` branch) — this exists
    for parity when DevOps runs independently.
    """
    if not req.approved:
        record_status(req.project_id, {**(get_status(req.project_id) or {}),
                                        "phase_status": "failed", "status": "failed"})
        return {"status": "rejected", "project_id": req.project_id}
    return {"status": "approved", "project_id": req.project_id}


@router.post("/rollback")
async def trigger_rollback(req: RollbackRequest) -> Dict[str, Any]:
    """Manual rollback trigger — normally rollback is automatic (see
    services.devops.context.decide_rollback), this is the operator escape hatch."""
    record_status(req.project_id, {**(get_status(req.project_id) or {}),
                                    "phase_status": "failed", "status": "rolled_back"})
    return {"status": "rollback_triggered", "project_id": req.project_id, "reason": req.reason}
