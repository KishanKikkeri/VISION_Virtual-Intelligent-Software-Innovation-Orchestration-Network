"""
services/repository/workflows/repository_graph.py
=====================================================
W-Repo: Repository Service LangGraph State Machine.

Phases (per the M3.2 handover):
  repository_request → validate → create_branch → commit → open_pr
  → approval → merge → release → publish_events

Unlike the agent-driven graphs (Product, Architecture), Repository
Service has no LLM in the loop — every node calls straight into a
manager (repository_manager / branch_manager / commit_manager /
pull_request_manager / release_manager), so this graph *is* the
durable execution engine for a single engineering.commit → PR → merge
→ release cycle, not a tracker for work happening elsewhere.

Failure handling:
  - Each action node retries up to `max_retries` (default 3) on a
    ProviderUnavailableError / transient failure.
  - Exhausting retries routes to escalate_node, which flags the task
    for human/manager attention.
  - escalate_node failing again (or a non-retryable error such as a
    ProtectedBranchViolationError) routes straight to dead_letter_node,
    which publishes to the `dlq.repository.*` subject — the DLQ path
    required by the handover.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog
from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from services.repository.managers import RepositoryDeps
from services.repository.managers.branch_manager import BranchManager
from services.repository.managers.commit_manager import CommitManager
from services.repository.managers.pull_request_manager import PullRequestManager
from services.repository.managers.release_manager import ReleaseManager
from services.repository.managers.repository_manager import RepositoryManager
from services.repository.schemas import (
    CommitFilesRequest,
    CommitMetadata,
    CreateBranchRequest,
    CreatePullRequestRequest,
    CreateReleaseRequest,
    FileChange,
    MergePullRequestRequest,
    ProtectedBranchViolationError,
    ApprovePullRequestRequest,
)

log = structlog.get_logger(__name__)

NON_RETRYABLE_EXCEPTIONS = (ProtectedBranchViolationError,)


class RepositoryState(TypedDict, total=False):
    project_id:   str
    workflow_id:  str
    task_id:      Optional[str]

    branch_type:  str            # feature|fix|hotfix
    incident_id:  Optional[str]
    slug:         Optional[str]
    base_branch:  Optional[str]

    title:            str
    description:      Optional[str]
    files:            List[Dict[str, Any]]
    commit_message:   str
    agent_id:         str
    lead_id:          Optional[str]
    reviewers:        List[str]

    create_release:   bool
    release_tag:      Optional[str]

    # Produced identifiers
    branch_name:        Optional[str]
    commit_sha:         Optional[str]
    pull_request_id:    Optional[str]
    provider_pr_number:  Optional[int]
    merge_sha:          Optional[str]

    # Approval gate
    awaiting_approval:  bool
    approval_status:    Optional[str]     # approved | rejected | None
    approval_feedback:  Optional[str]

    # Retry / failure handling
    retry_count:    int
    max_retries:    int
    phase_status:   str            # pending|running|awaiting_approval|completed|failed|dead_lettered
    failure_reason: Optional[str]
    retryable:      bool

    # Event queues
    nats_events_queue: List[Dict[str, Any]]
    ws_events_queue:   List[Dict[str, Any]]


def _ok(state: RepositoryState, **updates) -> Dict[str, Any]:
    return {"phase_status": "running", "failure_reason": None, **updates}


def _failed(exc: Exception) -> Dict[str, Any]:
    return {
        "phase_status": "failed",
        "failure_reason": str(exc),
        "retryable": not isinstance(exc, NON_RETRYABLE_EXCEPTIONS),
    }


# ── Routing ───────────────────────────────────────────────────

def _route_after_action(state: RepositoryState, success_target: str) -> str:
    if state.get("phase_status") == "failed":
        if not state.get("retryable", True):
            return "dead_letter"
        if state.get("retry_count", 0) < state.get("max_retries", 3):
            return "retry"
        return "escalate"
    return success_target


def route_after_validate(state: RepositoryState) -> str:
    return _route_after_action(state, "create_branch")


def route_after_create_branch(state: RepositoryState) -> str:
    return _route_after_action(state, "commit")


def route_after_commit(state: RepositoryState) -> str:
    return _route_after_action(state, "open_pr")


def route_after_open_pr(state: RepositoryState) -> str:
    return _route_after_action(state, "approval")


def route_approval_gate(state: RepositoryState) -> str:
    if state.get("approval_status") == "approved":
        return "approved"
    if state.get("approval_status") == "rejected":
        return "rejected"
    return "pending"


def route_after_merge(state: RepositoryState) -> str:
    if state.get("phase_status") == "failed":
        if not state.get("retryable", True):
            return "dead_letter"
        if state.get("retry_count", 0) < state.get("max_retries", 3):
            return "retry"
        return "escalate"
    return "release" if state.get("create_release") else "publish_events"


def route_after_release(state: RepositoryState) -> str:
    return _route_after_action(state, "publish_events")


def route_after_retry(state: RepositoryState) -> str:
    """Re-enters the node type that just failed, tracked via failure_reason prefix."""
    step = state.get("_failed_step", "validate")
    return step


# ── Graph builder ────────────────────────────────────────────

def build_repository_graph(deps: RepositoryDeps, checkpointer=None):
    """
    Builds W-Repo. One interrupt node: `approval`.
    Every action node is a thin wrapper around a manager call, so the
    graph is the single source of truth for retry/escalation/DLQ
    behaviour across the whole commit → PR → merge → release cycle.
    """
    repo_mgr = RepositoryManager(deps)
    branch_mgr = BranchManager(deps)
    commit_mgr = CommitManager(deps)
    pr_mgr = PullRequestManager(deps)
    release_mgr = ReleaseManager(deps)

    async def validate_node(state: RepositoryState) -> Dict[str, Any]:
        log.info("repo_graph_validate", project_id=state["project_id"])
        try:
            repo = await repo_mgr.get_repository(state["project_id"])
            if repo is None:
                from services.repository.schemas import CreateRepositoryRequest
                repo = await repo_mgr.create_repository(CreateRepositoryRequest(
                    project_id=state["project_id"],
                    project_name=state["project_id"],
                ))
            return _ok(state, _failed_step="validate")
        except Exception as exc:
            log.error("repo_graph_validate_failed", error=str(exc))
            return {**_failed(exc), "_failed_step": "validate"}

    async def create_branch_node(state: RepositoryState) -> Dict[str, Any]:
        log.info("repo_graph_create_branch", project_id=state["project_id"])
        try:
            from services.repository.schemas import BranchType
            branch = await branch_mgr.create_branch(CreateBranchRequest(
                project_id=state["project_id"],
                branch_type=BranchType(state["branch_type"]),
                task_id=state.get("task_id"),
                incident_id=state.get("incident_id"),
                slug=state.get("slug"),
                base_branch=state.get("base_branch"),
            ))
            return _ok(
                state, branch_name=branch.name, _failed_step="create_branch",
                nats_events_queue=[{"subject": "repository.branch.created",
                                    "payload": {"project_id": state["project_id"],
                                                "branch_name": branch.name}}],
            )
        except Exception as exc:
            log.error("repo_graph_create_branch_failed", error=str(exc))
            return {**_failed(exc), "_failed_step": "create_branch"}

    async def commit_node(state: RepositoryState) -> Dict[str, Any]:
        log.info("repo_graph_commit", project_id=state["project_id"])
        try:
            files = [FileChange(**f) for f in state.get("files", [])]
            commit = await commit_mgr.commit_files(CommitFilesRequest(
                project_id=state["project_id"],
                branch_name=state["branch_name"],
                message=state["commit_message"],
                files=files,
                metadata=CommitMetadata(
                    project_id=state["project_id"],
                    workflow_id=state["workflow_id"],
                    task_id=state.get("task_id", "unknown"),
                    agent_id=state.get("agent_id", "engineering-agent"),
                    lead_id=state.get("lead_id"),
                ),
            ))
            return _ok(
                state, commit_sha=commit.sha, _failed_step="commit",
                nats_events_queue=[{"subject": "repository.commit.created",
                                    "payload": {"project_id": state["project_id"],
                                                "sha": commit.sha}}],
            )
        except Exception as exc:
            log.error("repo_graph_commit_failed", error=str(exc))
            return {**_failed(exc), "_failed_step": "commit"}

    async def open_pr_node(state: RepositoryState) -> Dict[str, Any]:
        log.info("repo_graph_open_pr", project_id=state["project_id"])
        try:
            pr = await pr_mgr.create_pull_request(CreatePullRequestRequest(
                project_id=state["project_id"],
                source_branch=state["branch_name"],
                title=state["title"],
                description=state.get("description"),
                task_id=state.get("task_id"),
                reviewers=state.get("reviewers", []),
            ))
            return _ok(
                state, pull_request_id=pr.id, provider_pr_number=pr.provider_pr_number,
                awaiting_approval=True, _failed_step="open_pr",
                nats_events_queue=[{"subject": "repository.pr.created",
                                    "payload": {"project_id": state["project_id"],
                                                "pull_request_id": pr.id}}],
            )
        except Exception as exc:
            log.error("repo_graph_open_pr_failed", error=str(exc))
            return {**_failed(exc), "_failed_step": "open_pr"}

    async def approval_node(state: RepositoryState) -> Dict[str, Any]:
        """INTERRUPT NODE — pauses until the manager/reviewer injects a decision."""
        return {
            "awaiting_approval": True,
            "ws_events_queue": [{
                "project_id": state["project_id"],
                "event_type": "pr_approval_required",
                "payload": {"pull_request_id": state.get("pull_request_id")},
            }],
        }

    async def merge_node(state: RepositoryState) -> Dict[str, Any]:
        log.info("repo_graph_merge", project_id=state["project_id"])
        try:
            await pr_mgr.approve_pull_request(ApprovePullRequestRequest(
                project_id=state["project_id"],
                pull_request_id=state["pull_request_id"],
                approved_by=state.get("approval_feedback") or "manager-service",
            ))
            merged = await pr_mgr.merge_pull_request(MergePullRequestRequest(
                project_id=state["project_id"],
                pull_request_id=state["pull_request_id"],
            ))
            return _ok(
                state, merge_sha=merged.merge_sha, awaiting_approval=False,
                _failed_step="merge",
                nats_events_queue=[{"subject": "repository.pr.merged",
                                    "payload": {"project_id": state["project_id"],
                                                "merge_sha": merged.merge_sha}}],
            )
        except Exception as exc:
            log.error("repo_graph_merge_failed", error=str(exc))
            return {**_failed(exc), "_failed_step": "merge"}

    async def release_node(state: RepositoryState) -> Dict[str, Any]:
        log.info("repo_graph_release", project_id=state["project_id"])
        try:
            release = await release_mgr.create_release(CreateReleaseRequest(
                project_id=state["project_id"],
                tag_name=state["release_tag"],
            ))
            return _ok(
                state, _failed_step="release",
                nats_events_queue=[{"subject": "repository.release.created",
                                    "payload": {"project_id": state["project_id"],
                                                "tag_name": release.tag_name}}],
            )
        except Exception as exc:
            log.error("repo_graph_release_failed", error=str(exc))
            return {**_failed(exc), "_failed_step": "release"}

    async def publish_events_node(state: RepositoryState) -> Dict[str, Any]:
        log.info("repo_graph_publish_events", project_id=state["project_id"])
        return {
            "phase_status": "completed",
            "nats_events_queue": [{
                "subject": "repository.workflow.completed",
                "payload": {"project_id": state["project_id"],
                            "pull_request_id": state.get("pull_request_id"),
                            "merge_sha": state.get("merge_sha")},
            }],
        }

    async def retry_node(state: RepositoryState) -> Dict[str, Any]:
        retries = state.get("retry_count", 0) + 1
        log.warning("repo_graph_retry", project_id=state["project_id"],
                    attempt=retries, step=state.get("_failed_step"))
        return {"retry_count": retries, "phase_status": "running"}

    async def escalate_node(state: RepositoryState) -> Dict[str, Any]:
        log.warning("repo_graph_escalate", project_id=state["project_id"],
                    reason=state.get("failure_reason"))
        return {
            "phase_status": "escalated",
            "nats_events_queue": [{
                "subject": "repository.pipeline.escalated",
                "payload": {"project_id": state["project_id"],
                            "step": state.get("_failed_step"),
                            "reason": state.get("failure_reason")},
            }],
        }

    async def dead_letter_node(state: RepositoryState) -> Dict[str, Any]:
        log.error("repo_graph_dead_letter", project_id=state["project_id"],
                  reason=state.get("failure_reason"))
        return {
            "phase_status": "dead_lettered",
            "nats_events_queue": [{
                "subject": f"dlq.repository.{state.get('_failed_step', 'unknown')}",
                "payload": {"project_id": state["project_id"],
                            "step": state.get("_failed_step"),
                            "reason": state.get("failure_reason")},
            }],
        }

    g = StateGraph(RepositoryState)
    g.add_node("validate", validate_node)
    g.add_node("create_branch", create_branch_node)
    g.add_node("commit", commit_node)
    g.add_node("open_pr", open_pr_node)
    g.add_node("approval", approval_node)
    g.add_node("merge", merge_node)
    g.add_node("release", release_node)
    g.add_node("publish_events", publish_events_node)
    g.add_node("retry", retry_node)
    g.add_node("escalate", escalate_node)
    g.add_node("dead_letter", dead_letter_node)

    g.set_entry_point("validate")

    g.add_conditional_edges("validate", route_after_validate, {
        "create_branch": "create_branch", "retry": "retry",
        "escalate": "escalate", "dead_letter": "dead_letter",
    })
    g.add_conditional_edges("create_branch", route_after_create_branch, {
        "commit": "commit", "retry": "retry",
        "escalate": "escalate", "dead_letter": "dead_letter",
    })
    g.add_conditional_edges("commit", route_after_commit, {
        "open_pr": "open_pr", "retry": "retry",
        "escalate": "escalate", "dead_letter": "dead_letter",
    })
    g.add_conditional_edges("open_pr", route_after_open_pr, {
        "approval": "approval", "retry": "retry",
        "escalate": "escalate", "dead_letter": "dead_letter",
    })
    g.add_conditional_edges("approval", route_approval_gate, {
        "approved": "merge", "rejected": "publish_events", "pending": "approval",
    })
    g.add_conditional_edges("merge", route_after_merge, {
        "release": "release", "publish_events": "publish_events",
        "retry": "retry", "escalate": "escalate", "dead_letter": "dead_letter",
    })
    g.add_conditional_edges("release", route_after_release, {
        "publish_events": "publish_events", "retry": "retry",
        "escalate": "escalate", "dead_letter": "dead_letter",
    })
    g.add_conditional_edges("retry", route_after_retry, {
        "validate": "validate", "create_branch": "create_branch",
        "commit": "commit", "open_pr": "open_pr", "merge": "merge",
        "release": "release",
    })

    g.add_edge("publish_events", END)
    g.add_edge("escalate", END)
    g.add_edge("dead_letter", END)

    kwargs: Dict[str, Any] = {"interrupt_before": ["approval"]}
    if checkpointer:
        kwargs["checkpointer"] = checkpointer

    return g.compile(**kwargs)
