"""
services/repository/managers/release_manager.py
==================================================
Release Manager.
Responsibilities: tags, releases, rollback metadata.

Releases have no dedicated table (see Database Additions in the M3.2
handover — only repositories/branches/pull_requests/repository_events
are added). Release and rollback history instead lives in the
append-only repository_events log, keyed by tag_name, which is exactly
what "rollback metadata" calls for: a compliance trail, not a mutable
row that gets overwritten on every release.
"""
from __future__ import annotations

from typing import List

import structlog

from infrastructure.database.repositories import RepositoryRepository
from services.repository.managers import RepositoryDeps
from services.repository.managers.audit_manager import AuditManager
from services.repository.schemas import (
    CreateReleaseRequest,
    ReleaseResponse,
    RepositoryEventResponse,
    RepositoryEventType,
    RepositoryServiceError,
    RollbackReleaseRequest,
)

log = structlog.get_logger(__name__)


class ReleaseManager:
    def __init__(self, deps: RepositoryDeps) -> None:
        self.deps = deps
        self.audit = AuditManager(deps)

    async def _get_repository_row(self, project_id: str):
        async with self.deps.db_factory() as db:
            repo = await RepositoryRepository.get_by_project(db, project_id)
        if repo is None:
            raise RepositoryServiceError(f"No repository provisioned for project {project_id}")
        return repo

    async def create_release(self, req: CreateReleaseRequest) -> ReleaseResponse:
        repo = await self._get_repository_row(req.project_id)
        target = req.target_branch or repo.default_branch

        provider_result = await self.deps.provider.create_release(
            owner=repo.owner, repo=repo.name, tag_name=req.tag_name,
            target_commitish=target, name=req.name, body=req.body,
            prerelease=req.prerelease,
        )

        await self.audit.record_and_publish(
            event_type=RepositoryEventType.RELEASE_CREATED.value,
            repository_id=repo.id, project_id=req.project_id,
            entity_type="release", entity_id=req.tag_name,
            payload={"tag_name": req.tag_name, "target_branch": target,
                     "prerelease": req.prerelease, "html_url": provider_result.html_url},
        )
        return ReleaseResponse(
            tag_name=provider_result.tag_name, name=req.name or req.tag_name,
            html_url=provider_result.html_url, target_sha=provider_result.target_sha,
            prerelease=req.prerelease,
        )

    async def rollback_release(self, req: RollbackReleaseRequest) -> None:
        """
        Non-destructive rollback: retracts the GitHub *release* (not the
        underlying git tag or any commit history) and records why.
        Git history is never rewritten and no force push occurs.
        """
        repo = await self._get_repository_row(req.project_id)

        await self.deps.provider.delete_release(owner=repo.owner, repo=repo.name,
                                                tag_name=req.tag_name)

        await self.audit.record_and_publish(
            event_type=RepositoryEventType.RELEASE_ROLLBACK.value,
            repository_id=repo.id, project_id=req.project_id,
            entity_type="release", entity_id=req.tag_name,
            payload={"tag_name": req.tag_name, "reason": req.reason},
        )

    async def list_release_history(self, project_id: str) -> List[RepositoryEventResponse]:
        repo = await self._get_repository_row(project_id)
        rows = await self.audit.list_events(repo.id, limit=200)
        release_types = {
            RepositoryEventType.RELEASE_CREATED.value,
            RepositoryEventType.RELEASE_ROLLBACK.value,
        }
        return [
            RepositoryEventResponse.model_validate(r)
            for r in rows if r.event_type in release_types
        ]
