"""
services/repository/managers/commit_manager.py
=================================================
Commit Manager.
Responsibilities: commit formatting, commit creation, audit linkage.

Commit ownership (locked):
  - Every commit is authored by "VISION Bot".
  - Every commit message carries a metadata trailer block with
    Project-Id, Workflow-Id, Task-Id, Agent-Id, and (optionally) Lead-Id.
  - Direct commits to main/develop are refused — Engineering agents
    must go through a feature/fix/hotfix branch and a pull request.
"""
from __future__ import annotations

import structlog

from infrastructure.database.repositories import BranchRepository, RepositoryRepository
from services.repository.managers import RepositoryDeps, assert_not_protected
from services.repository.managers.audit_manager import AuditManager
from services.repository.providers.github_provider import VISION_BOT_EMAIL, VISION_BOT_NAME
from services.repository.schemas import (
    CommitFilesRequest,
    CommitResponse,
    RepositoryEventType,
    RepositoryServiceError,
)

log = structlog.get_logger(__name__)


def format_commit_message(message: str, metadata) -> str:
    """Subject line, blank line, then the locked metadata trailer block."""
    subject = message.strip().splitlines()[0] if message.strip() else "chore: update"
    return f"{subject}\n\n{metadata.as_trailer_block()}"


class CommitManager:
    def __init__(self, deps: RepositoryDeps) -> None:
        self.deps = deps
        self.audit = AuditManager(deps)

    async def commit_files(self, req: CommitFilesRequest) -> CommitResponse:
        assert_not_protected(req.branch_name, action="commit directly to")

        async with self.deps.db_factory() as db:
            repo = await RepositoryRepository.get_by_project(db, req.project_id)
        if repo is None:
            raise RepositoryServiceError(f"No repository provisioned for project {req.project_id}")

        async with self.deps.db_factory() as db:
            branch = await BranchRepository.get_by_name(db, repo.id, req.branch_name)
        if branch is None:
            raise RepositoryServiceError(
                f"Branch '{req.branch_name}' is not tracked for project {req.project_id}"
            )

        full_message = format_commit_message(req.message, req.metadata)

        provider_result = await self.deps.provider.commit_files(
            owner=repo.owner, repo=repo.name, branch_name=req.branch_name,
            message=full_message, files=req.files,
            author_name=VISION_BOT_NAME, author_email=VISION_BOT_EMAIL,
        )

        async with self.deps.db_factory() as db:
            await BranchRepository.update_head(db, branch.id, provider_result.sha)

        await self.audit.record_and_publish(
            event_type=RepositoryEventType.COMMIT_CREATED.value,
            repository_id=repo.id, project_id=req.project_id,
            entity_type="commit", entity_id=provider_result.sha,
            payload={
                "branch_name": req.branch_name,
                "file_count": len(req.files),
                "task_id": req.metadata.task_id,
                "agent_id": req.metadata.agent_id,
            },
        )
        return CommitResponse(
            sha=provider_result.sha, message=full_message,
            branch_name=req.branch_name, html_url=provider_result.html_url,
        )
