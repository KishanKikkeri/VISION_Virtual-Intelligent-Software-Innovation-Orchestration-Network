"""
services/repository/managers/pull_request_manager.py
=======================================================
Pull Request Manager.
Responsibilities: create PR, assign reviewers, approve, merge.

Merge strategy (locked): squash only. Merge commits and rebase merges
are disabled — merge_pull_request() always calls the provider's
squash-merge path and refuses to merge anything not yet approved.
"""
from __future__ import annotations

from typing import List, Optional

import structlog

from infrastructure.database.repositories import PullRequestRepository, RepositoryRepository
from services.repository.managers import RepositoryDeps
from services.repository.managers.audit_manager import AuditManager
from services.repository.managers.branch_manager import BranchManager
from services.repository.schemas import (
    ApprovePullRequestRequest,
    CreatePullRequestRequest,
    MergeConflictError,
    MergePullRequestRequest,
    PermissionDeniedError,
    PullRequestResponse,
    RepositoryEventType,
    RepositoryServiceError,
)

log = structlog.get_logger(__name__)


class PullRequestManager:
    def __init__(self, deps: RepositoryDeps) -> None:
        self.deps = deps
        self.audit = AuditManager(deps)
        self.branches = BranchManager(deps)

    async def _get_repository_row(self, project_id: str):
        async with self.deps.db_factory() as db:
            repo = await RepositoryRepository.get_by_project(db, project_id)
        if repo is None:
            raise RepositoryServiceError(f"No repository provisioned for project {project_id}")
        return repo

    async def create_pull_request(self, req: CreatePullRequestRequest) -> PullRequestResponse:
        repo = await self._get_repository_row(req.project_id)
        target = req.target_branch or "develop"

        provider_result = await self.deps.provider.create_pull_request(
            owner=repo.owner, repo=repo.name, title=req.title, body=req.description,
            head=req.source_branch, base=target, reviewers=req.reviewers,
        )

        async with self.deps.db_factory() as db:
            row = await PullRequestRepository.create(
                db, repository_id=repo.id, title=req.title,
                source_branch=req.source_branch, target_branch=target,
                description=req.description, task_id=req.task_id,
                provider_pr_number=provider_result.number,
                reviewers=req.reviewers, html_url=provider_result.html_url,
            )
            response = PullRequestResponse.model_validate(row)

        await self.audit.record_and_publish(
            event_type=RepositoryEventType.PR_CREATED.value,
            repository_id=repo.id, project_id=req.project_id,
            entity_type="pull_request", entity_id=response.id,
            payload={"provider_pr_number": provider_result.number,
                     "source_branch": req.source_branch, "target_branch": target},
        )
        return response

    async def approve_pull_request(self, req: ApprovePullRequestRequest) -> PullRequestResponse:
        async with self.deps.db_factory() as db:
            pr = await PullRequestRepository.get_by_id(db, req.pull_request_id)
        if pr is None:
            raise RepositoryServiceError(f"Pull request {req.pull_request_id} not found")
        if pr.status != "open":
            raise RepositoryServiceError(
                f"Pull request {req.pull_request_id} is '{pr.status}', not 'open'"
            )

        async with self.deps.db_factory() as db:
            await PullRequestRepository.mark_approved(db, pr.id)
            updated = await PullRequestRepository.get_by_id(db, pr.id)

        await self.audit.record_and_publish(
            event_type=RepositoryEventType.PR_APPROVED.value,
            repository_id=pr.repository_id, project_id=req.project_id,
            entity_type="pull_request", entity_id=pr.id,
            payload={"approved_by": req.approved_by},
            actor=req.approved_by,
        )
        return PullRequestResponse.model_validate(updated)

    async def merge_pull_request(self, req: MergePullRequestRequest) -> PullRequestResponse:
        async with self.deps.db_factory() as db:
            pr = await PullRequestRepository.get_by_id(db, req.pull_request_id)
        if pr is None:
            raise RepositoryServiceError(f"Pull request {req.pull_request_id} not found")

        # Approval gate can never be bypassed, even by a retried/duplicate call.
        if pr.status != "approved":
            raise PermissionDeniedError(
                f"Pull request {req.pull_request_id} must be approved before merge "
                f"(current status: '{pr.status}')"
            )

        async with self.deps.db_factory() as db:
            repo = await RepositoryRepository.get_by_id(db, pr.repository_id)

        commit_message = f"{pr.title} (#{pr.provider_pr_number})"
        try:
            merge_sha = await self.deps.provider.merge_pull_request(
                owner=repo.owner, repo=repo.name,
                number=pr.provider_pr_number, commit_message=commit_message,
            )
        except MergeConflictError:
            async with self.deps.db_factory() as db:
                await PullRequestRepository.mark_conflicted(db, pr.id)
            await self.audit.record_and_publish(
                event_type="pr.merge_conflict", repository_id=pr.repository_id,
                project_id=req.project_id, entity_type="pull_request", entity_id=pr.id,
                payload={"provider_pr_number": pr.provider_pr_number},
            )
            raise

        async with self.deps.db_factory() as db:
            await PullRequestRepository.mark_merged(db, pr.id, merge_sha)
            updated = await PullRequestRepository.get_by_id(db, pr.id)

        # Post-merge housekeeping: retire the source branch (never main/develop).
        try:
            branch_row = None
            async with self.deps.db_factory() as db:
                from infrastructure.database.repositories import BranchRepository
                branch_row = await BranchRepository.get_by_name(db, repo.id, pr.source_branch)
            if branch_row and pr.source_branch not in ("main", "develop"):
                await self.branches.mark_merged(branch_row.id)
        except Exception as exc:
            log.warning("post_merge_branch_cleanup_failed", error=str(exc))

        await self.audit.record_and_publish(
            event_type=RepositoryEventType.PR_MERGED.value,
            repository_id=pr.repository_id, project_id=req.project_id,
            entity_type="pull_request", entity_id=pr.id,
            payload={"merge_sha": merge_sha, "merge_strategy": "squash"},
        )
        return PullRequestResponse.model_validate(updated)

    async def list_pull_requests(self, project_id: str, status: Optional[str] = None) -> List[PullRequestResponse]:
        repo = await self._get_repository_row(project_id)
        async with self.deps.db_factory() as db:
            rows = await PullRequestRepository.list_for_repository(db, repo.id, status=status)
        return [PullRequestResponse.model_validate(r) for r in rows]
