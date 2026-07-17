"""
services/repository/api/events.py
====================================
NATS event bindings for Repository Service.

Subscribes (per the M3.2 handover):
  engineering.commit.requested
  engineering.pr.requested
  engineering.release.requested

Publishes (all emitted by AuditManager.record_and_publish — see
managers/audit_manager.py — so every publish is paired with an
append-only repository_events row):
  repository.created         repository.branch.created
  repository.commit.created  repository.pr.created
  repository.pr.approved     repository.pr.merged
  repository.release.created repository.release.rollback

(Namespaced under `repository.*` as of the M3.9 messaging cleanup —
these previously published un-namespaced, e.g. "branch.created", which
meant they silently never matched Monitoring's `repository.>` wildcard
consumer despite looking like they should. See
docs/M3.9_Platform_Integration_Handover.md §6 and the messaging-cleanup
follow-up for the full history.)
"""
from __future__ import annotations

from typing import Any, Dict

import structlog

from infrastructure.messaging.nats_client import NATSClient
from services.repository.managers import RepositoryDeps
from services.repository.managers.commit_manager import CommitManager
from services.repository.managers.pull_request_manager import PullRequestManager
from services.repository.managers.release_manager import ReleaseManager
from services.repository.schemas import (
    CommitFilesRequest,
    CommitMetadata,
    CreatePullRequestRequest,
    CreateReleaseRequest,
    FileChange,
    RepositoryServiceError,
)

log = structlog.get_logger(__name__)


async def _on_commit_requested(deps: RepositoryDeps, payload: Dict[str, Any]) -> None:
    try:
        req = CommitFilesRequest(
            project_id=payload["project_id"],
            branch_name=payload["branch_name"],
            message=payload.get("message", "chore: automated commit"),
            files=[FileChange(**f) for f in payload.get("files", [])],
            metadata=CommitMetadata(
                project_id=payload["project_id"],
                workflow_id=payload.get("workflow_id", "unknown"),
                task_id=payload.get("task_id", "unknown"),
                agent_id=payload.get("agent_id", "engineering-agent"),
                lead_id=payload.get("lead_id"),
            ),
        )
        await CommitManager(deps).commit_files(req)
    except RepositoryServiceError as exc:
        log.error("engineering_commit_requested_failed", error=str(exc), payload=payload)


async def _on_pr_requested(deps: RepositoryDeps, payload: Dict[str, Any]) -> None:
    try:
        req = CreatePullRequestRequest(
            project_id=payload["project_id"],
            source_branch=payload["source_branch"],
            target_branch=payload.get("target_branch"),
            title=payload.get("title", "Automated pull request"),
            description=payload.get("description"),
            task_id=payload.get("task_id"),
            reviewers=payload.get("reviewers", []),
        )
        await PullRequestManager(deps).create_pull_request(req)
    except RepositoryServiceError as exc:
        log.error("engineering_pr_requested_failed", error=str(exc), payload=payload)


async def _on_release_requested(deps: RepositoryDeps, payload: Dict[str, Any]) -> None:
    try:
        req = CreateReleaseRequest(
            project_id=payload["project_id"],
            tag_name=payload["tag_name"],
            target_branch=payload.get("target_branch"),
            name=payload.get("name"),
            body=payload.get("body"),
            prerelease=payload.get("prerelease", False),
        )
        await ReleaseManager(deps).create_release(req)
    except RepositoryServiceError as exc:
        log.error("engineering_release_requested_failed", error=str(exc), payload=payload)


async def setup_repository_subscriptions(nats: NATSClient, deps: RepositoryDeps) -> None:
    """Called once from main.py's startup lifespan."""

    async def commit_handler(payload: Dict[str, Any]) -> None:
        await _on_commit_requested(deps, payload)

    async def pr_handler(payload: Dict[str, Any]) -> None:
        await _on_pr_requested(deps, payload)

    async def release_handler(payload: Dict[str, Any]) -> None:
        await _on_release_requested(deps, payload)

    await nats.subscribe("engineering.commit.requested", commit_handler,
                         durable="repository-commit-requested")
    await nats.subscribe("engineering.pr.requested", pr_handler,
                         durable="repository-pr-requested")
    await nats.subscribe("engineering.release.requested", release_handler,
                         durable="repository-release-requested")
    log.info("repository_subscriptions_ready")
