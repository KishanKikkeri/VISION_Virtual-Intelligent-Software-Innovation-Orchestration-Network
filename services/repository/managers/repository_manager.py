"""
services/repository/managers/repository_manager.py
=====================================================
Repository Manager.
Responsibilities: repository creation, metadata, lookup.

Every project gets exactly one monorepo, laid out as:
  frontend/  backend/  infrastructure/  tests/  docs/  .vision/
Both protected branches (main, develop) exist before this returns.
"""
from __future__ import annotations

from typing import Optional

import structlog

from core.config.settings import get_settings
from infrastructure.database.repositories import RepositoryRepository
from services.repository.managers import RepositoryDeps, slugify
from services.repository.managers.audit_manager import AuditManager
from services.repository.schemas import (
    CreateRepositoryRequest,
    RepositoryEventType,
    RepositoryResponse,
    RepositoryServiceError,
)

log = structlog.get_logger(__name__)

MONOREPO_LAYOUT = (
    "frontend/.gitkeep",
    "backend/.gitkeep",
    "infrastructure/.gitkeep",
    "tests/.gitkeep",
    "docs/.gitkeep",
    ".vision/.gitkeep",
)


class RepositoryManager:
    def __init__(self, deps: RepositoryDeps) -> None:
        self.deps = deps
        self.audit = AuditManager(deps)

    async def create_repository(self, req: CreateRepositoryRequest) -> RepositoryResponse:
        settings = get_settings()
        owner = req.owner or self.deps.default_owner or settings.github_default_owner
        if not owner:
            raise RepositoryServiceError(
                "No repository owner configured — set owner on the request "
                "or GITHUB_DEFAULT_OWNER"
            )

        # Idempotency: one repository per project.
        async with self.deps.db_factory() as db:
            existing = await RepositoryRepository.get_by_project(db, req.project_id)
        if existing is not None:
            return RepositoryResponse.model_validate(existing)

        repo_name = slugify(req.project_name)

        provider_result = await self.deps.provider.create_repository(
            owner=owner,
            name=repo_name,
            description=req.description,
            visibility=req.visibility.value,
        )

        # `develop` protected branch, off the auto-initialised default branch.
        await self.deps.provider.create_branch(
            owner=provider_result.owner,
            repo=provider_result.name,
            branch_name="develop",
            base_branch=provider_result.default_branch,
        )

        # Seed the monorepo layout on develop so Engineering has a stable base.
        from services.repository.schemas import FileChange
        await self.deps.provider.commit_files(
            owner=provider_result.owner,
            repo=provider_result.name,
            branch_name="develop",
            message=(
                "chore: scaffold monorepo layout\n\n"
                f"Project-Id: {req.project_id}\n"
                "Agent-Id: repository-service"
            ),
            files=[FileChange(path=p, content="") for p in MONOREPO_LAYOUT],
        )

        async with self.deps.db_factory() as db:
            row = await RepositoryRepository.create(
                db,
                project_id=req.project_id,
                provider=self.deps.provider.name,
                owner=provider_result.owner,
                name=provider_result.name,
                full_name=provider_result.full_name,
                default_branch=provider_result.default_branch,
                clone_url=provider_result.clone_url,
                html_url=provider_result.html_url,
                visibility=provider_result.visibility,
                provider_repo_id=provider_result.provider_repo_id,
                metadata={"description": req.description or ""},
            )
            response = RepositoryResponse.model_validate(row)

        await self.audit.record_and_publish(
            event_type=RepositoryEventType.REPOSITORY_CREATED.value,
            repository_id=response.id,
            project_id=req.project_id,
            entity_type="repository",
            entity_id=response.id,
            payload={"full_name": response.full_name, "html_url": response.html_url},
        )
        return response

    async def get_repository(self, project_id: str) -> Optional[RepositoryResponse]:
        async with self.deps.db_factory() as db:
            row = await RepositoryRepository.get_by_project(db, project_id)
        return RepositoryResponse.model_validate(row) if row else None

    async def get_repository_by_id(self, repository_id: str) -> Optional[RepositoryResponse]:
        async with self.deps.db_factory() as db:
            row = await RepositoryRepository.get_by_id(db, repository_id)
        return RepositoryResponse.model_validate(row) if row else None
