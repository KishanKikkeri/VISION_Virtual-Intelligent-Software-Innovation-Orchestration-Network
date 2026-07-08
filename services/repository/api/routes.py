"""
services/repository/api/routes.py
====================================
Repository Service's public surface. Engineering agents (and every
other department) talk to Repository Service exclusively through
these HTTP endpoints — never through git commands or a provider SDK.

Required operations (per the M3.2 handover), all present below:
  create_repository()   create_branch()        commit_files()
  create_pull_request()  approve_pull_request()  merge_pull_request()
  create_release()       rollback_release()
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException

from services.repository.managers import RepositoryDeps
from services.repository.managers.branch_manager import BranchManager
from services.repository.managers.commit_manager import CommitManager
from services.repository.managers.pull_request_manager import PullRequestManager
from services.repository.managers.release_manager import ReleaseManager
from services.repository.managers.repository_manager import RepositoryManager
from services.repository.managers.audit_manager import AuditManager
from services.repository.schemas import (
    ApprovePullRequestRequest,
    BranchResponse,
    CommitFilesRequest,
    CommitResponse,
    CreateBranchRequest,
    CreatePullRequestRequest,
    CreateReleaseRequest,
    CreateRepositoryRequest,
    MergePullRequestRequest,
    MergeConflictError,
    PermissionDeniedError,
    ProtectedBranchViolationError,
    ProviderUnavailableError,
    PullRequestResponse,
    ReleaseResponse,
    RepositoryEventResponse,
    RepositoryResponse,
    RepositoryServiceError,
    RollbackReleaseRequest,
)

log = structlog.get_logger(__name__)
router = APIRouter()

# ── Dependency wiring ─────────────────────────────────────────
# Set once by services/repository/main.py during startup lifespan.
_deps: Optional[RepositoryDeps] = None


def set_deps(deps: RepositoryDeps) -> None:
    global _deps
    _deps = deps


def get_deps() -> RepositoryDeps:
    if _deps is None:
        raise HTTPException(503, "Repository Service not initialised")
    return _deps


def _http_error(exc: RepositoryServiceError) -> HTTPException:
    if isinstance(exc, (PermissionDeniedError, ProtectedBranchViolationError)):
        return HTTPException(403, str(exc))
    if isinstance(exc, MergeConflictError):
        return HTTPException(409, str(exc))
    if isinstance(exc, ProviderUnavailableError):
        return HTTPException(503, str(exc))
    return HTTPException(422, str(exc))


# ── Health ────────────────────────────────────────────────────

@router.get("/health", tags=["System"])
async def health(deps: RepositoryDeps = Depends(get_deps)) -> Dict[str, Any]:
    from infrastructure.database.connection import check_db_health
    return {
        "status": "ok",
        "service": "repository",
        "provider": getattr(deps.provider, "name", "unknown"),
        "db": await check_db_health(),
    }


# ── Repository lifecycle ─────────────────────────────────────

@router.post("/repositories", response_model=RepositoryResponse, tags=["Repository"])
async def create_repository(
    req: CreateRepositoryRequest, deps: RepositoryDeps = Depends(get_deps),
) -> RepositoryResponse:
    try:
        return await RepositoryManager(deps).create_repository(req)
    except RepositoryServiceError as exc:
        raise _http_error(exc)


@router.get("/repositories/{project_id}", response_model=RepositoryResponse, tags=["Repository"])
async def get_repository(
    project_id: str, deps: RepositoryDeps = Depends(get_deps),
) -> RepositoryResponse:
    repo = await RepositoryManager(deps).get_repository(project_id)
    if repo is None:
        raise HTTPException(404, f"No repository provisioned for project {project_id}")
    return repo


# ── Branch lifecycle ─────────────────────────────────────────

@router.post("/branches", response_model=BranchResponse, tags=["Branch"])
async def create_branch(
    req: CreateBranchRequest, deps: RepositoryDeps = Depends(get_deps),
) -> BranchResponse:
    try:
        return await BranchManager(deps).create_branch(req)
    except RepositoryServiceError as exc:
        raise _http_error(exc)


@router.get("/branches/{project_id}", response_model=List[BranchResponse], tags=["Branch"])
async def list_branches(
    project_id: str, status: Optional[str] = None, deps: RepositoryDeps = Depends(get_deps),
) -> List[BranchResponse]:
    try:
        return await BranchManager(deps).list_branches(project_id, status=status)
    except RepositoryServiceError as exc:
        raise _http_error(exc)


@router.delete("/branches/{project_id}/{branch_name:path}", tags=["Branch"])
async def delete_branch(
    project_id: str, branch_name: str, deps: RepositoryDeps = Depends(get_deps),
) -> Dict[str, str]:
    try:
        await BranchManager(deps).delete_branch(project_id, branch_name)
        return {"status": "deleted", "branch_name": branch_name}
    except RepositoryServiceError as exc:
        raise _http_error(exc)


# ── Commit lifecycle ──────────────────────────────────────────

@router.post("/commits", response_model=CommitResponse, tags=["Commit"])
async def commit_files(
    req: CommitFilesRequest, deps: RepositoryDeps = Depends(get_deps),
) -> CommitResponse:
    try:
        return await CommitManager(deps).commit_files(req)
    except RepositoryServiceError as exc:
        raise _http_error(exc)


# ── Pull request lifecycle ────────────────────────────────────

@router.post("/pull-requests", response_model=PullRequestResponse, tags=["PullRequest"])
async def create_pull_request(
    req: CreatePullRequestRequest, deps: RepositoryDeps = Depends(get_deps),
) -> PullRequestResponse:
    try:
        return await PullRequestManager(deps).create_pull_request(req)
    except RepositoryServiceError as exc:
        raise _http_error(exc)


@router.get("/pull-requests/{project_id}", response_model=List[PullRequestResponse], tags=["PullRequest"])
async def list_pull_requests(
    project_id: str, status: Optional[str] = None, deps: RepositoryDeps = Depends(get_deps),
) -> List[PullRequestResponse]:
    try:
        return await PullRequestManager(deps).list_pull_requests(project_id, status=status)
    except RepositoryServiceError as exc:
        raise _http_error(exc)


@router.post("/pull-requests/approve", response_model=PullRequestResponse, tags=["PullRequest"])
async def approve_pull_request(
    req: ApprovePullRequestRequest, deps: RepositoryDeps = Depends(get_deps),
) -> PullRequestResponse:
    try:
        return await PullRequestManager(deps).approve_pull_request(req)
    except RepositoryServiceError as exc:
        raise _http_error(exc)


@router.post("/pull-requests/merge", response_model=PullRequestResponse, tags=["PullRequest"])
async def merge_pull_request(
    req: MergePullRequestRequest, deps: RepositoryDeps = Depends(get_deps),
) -> PullRequestResponse:
    try:
        return await PullRequestManager(deps).merge_pull_request(req)
    except RepositoryServiceError as exc:
        raise _http_error(exc)


# ── Release lifecycle ─────────────────────────────────────────

@router.post("/releases", response_model=ReleaseResponse, tags=["Release"])
async def create_release(
    req: CreateReleaseRequest, deps: RepositoryDeps = Depends(get_deps),
) -> ReleaseResponse:
    try:
        return await ReleaseManager(deps).create_release(req)
    except RepositoryServiceError as exc:
        raise _http_error(exc)


@router.post("/releases/rollback", tags=["Release"])
async def rollback_release(
    req: RollbackReleaseRequest, deps: RepositoryDeps = Depends(get_deps),
) -> Dict[str, str]:
    try:
        await ReleaseManager(deps).rollback_release(req)
        return {"status": "rolled_back", "tag_name": req.tag_name}
    except RepositoryServiceError as exc:
        raise _http_error(exc)


@router.get("/releases/{project_id}/history", response_model=List[RepositoryEventResponse], tags=["Release"])
async def release_history(
    project_id: str, deps: RepositoryDeps = Depends(get_deps),
) -> List[RepositoryEventResponse]:
    try:
        return await ReleaseManager(deps).list_release_history(project_id)
    except RepositoryServiceError as exc:
        raise _http_error(exc)


# ── Audit ──────────────────────────────────────────────────────

@router.get("/events/{repository_id}", response_model=List[RepositoryEventResponse], tags=["Audit"])
async def list_events(
    repository_id: str, limit: int = 100, deps: RepositoryDeps = Depends(get_deps),
) -> List[RepositoryEventResponse]:
    rows = await AuditManager(deps).list_events(repository_id, limit=limit)
    return [RepositoryEventResponse.model_validate(r) for r in rows]
