"""
services/repository/managers/branch_manager.py
=================================================
Branch Manager.
Responsibilities: branch creation, branch validation, naming policy.

Generated branches (locked):
  feature/<task-id>-<slug>
  fix/<task-id>-<slug>
  hotfix/<incident-id>
`main` and `develop` are protected — this manager never creates,
deletes, or force-pushes them.
"""
from __future__ import annotations

from typing import List, Optional

import structlog

from infrastructure.database.repositories import BranchRepository, RepositoryRepository
from services.repository.managers import (
    RepositoryDeps, assert_not_protected, build_branch_name,
)
from services.repository.managers.audit_manager import AuditManager
from services.repository.schemas import (
    BranchResponse,
    CreateBranchRequest,
    RepositoryEventType,
    RepositoryServiceError,
)

log = structlog.get_logger(__name__)


class BranchManager:
    def __init__(self, deps: RepositoryDeps) -> None:
        self.deps = deps
        self.audit = AuditManager(deps)

    async def _get_repository_row(self, project_id: str):
        async with self.deps.db_factory() as db:
            repo = await RepositoryRepository.get_by_project(db, project_id)
        if repo is None:
            raise RepositoryServiceError(f"No repository provisioned for project {project_id}")
        return repo

    async def create_branch(self, req: CreateBranchRequest) -> BranchResponse:
        repo = await self._get_repository_row(req.project_id)

        branch_name = build_branch_name(
            branch_type=req.branch_type.value,
            task_id=req.task_id,
            incident_id=req.incident_id,
            slug=req.slug,
        )
        assert_not_protected(branch_name, action="create")

        base_branch = req.base_branch or (
            repo.default_branch if req.branch_type.value == "hotfix" else "develop"
        )

        provider_result = await self.deps.provider.create_branch(
            owner=repo.owner, repo=repo.name,
            branch_name=branch_name, base_branch=base_branch,
        )

        async with self.deps.db_factory() as db:
            row = await BranchRepository.create(
                db, repository_id=repo.id, name=provider_result.name,
                branch_type=req.branch_type.value, task_id=req.task_id or req.incident_id,
                base_branch=base_branch, head_sha=provider_result.head_sha,
                is_protected=False,
            )
            response = BranchResponse.model_validate(row)

        await self.audit.record_and_publish(
            event_type=RepositoryEventType.BRANCH_CREATED.value,
            repository_id=repo.id, project_id=req.project_id,
            entity_type="branch", entity_id=response.id,
            payload={"name": branch_name, "base_branch": base_branch},
        )
        return response

    async def list_branches(self, project_id: str, status: Optional[str] = None) -> List[BranchResponse]:
        repo = await self._get_repository_row(project_id)
        async with self.deps.db_factory() as db:
            rows = await BranchRepository.list_for_repository(db, repo.id, status=status)
        return [BranchResponse.model_validate(r) for r in rows]

    async def mark_merged(self, branch_id: str) -> None:
        async with self.deps.db_factory() as db:
            await BranchRepository.mark_merged(db, branch_id)

    async def delete_branch(self, project_id: str, branch_name: str) -> None:
        """Deletes a non-protected branch, e.g. post-merge cleanup."""
        assert_not_protected(branch_name, action="delete")
        repo = await self._get_repository_row(project_id)

        await self.deps.provider.delete_branch(owner=repo.owner, repo=repo.name,
                                               branch_name=branch_name)

        async with self.deps.db_factory() as db:
            row = await BranchRepository.get_by_name(db, repo.id, branch_name)
            if row:
                await BranchRepository.mark_deleted(db, row.id)
