"""
services/repository/schemas/__init__.py
=========================================
M3.2 — Repository Service Schemas.
Every request, response, and provider-result type used by Repository
Service lives here. Nothing in managers/, providers/, api/, or
workflows/ should define its own ad-hoc dict shape — import from here.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Enums ───────────────────────────────────────────────────────

class BranchType(str, Enum):
    PROTECTED   = "protected"
    FEATURE     = "feature"
    FIX         = "fix"
    HOTFIX      = "hotfix"
    INTEGRATION = "integration"   # Appendix A (M3.3) — owned by Engineering Lead


class RepositoryVisibility(str, Enum):
    PRIVATE  = "private"
    INTERNAL = "internal"
    PUBLIC   = "public"


class PullRequestStatus(str, Enum):
    OPEN       = "open"
    APPROVED   = "approved"
    MERGED     = "merged"
    CLOSED     = "closed"
    CONFLICTED = "conflicted"


class RepositoryEventType(str, Enum):
    REPOSITORY_CREATED = "repository.created"
    BRANCH_CREATED      = "repository.branch.created"
    COMMIT_CREATED      = "repository.commit.created"
    PR_CREATED          = "repository.pr.created"
    PR_APPROVED         = "repository.pr.approved"
    PR_MERGED           = "repository.pr.merged"
    RELEASE_CREATED     = "repository.release.created"
    RELEASE_ROLLBACK    = "repository.release.rollback"


# ── Commit metadata (embedded in every VISION Bot commit) ───────

class CommitMetadata(BaseModel):
    project_id:  str
    workflow_id: str
    task_id:     str
    agent_id:    str
    lead_id:     Optional[str] = None

    def as_trailer_block(self) -> str:
        lines = [
            f"Project-Id: {self.project_id}",
            f"Workflow-Id: {self.workflow_id}",
            f"Task-Id: {self.task_id}",
            f"Agent-Id: {self.agent_id}",
        ]
        if self.lead_id:
            lines.append(f"Lead-Id: {self.lead_id}")
        return "\n".join(lines)


class FileChange(BaseModel):
    path:    str
    content: str
    mode:    str = "100644"   # git file mode; 100644 = regular file


# ── Requests ─────────────────────────────────────────────────────

class CreateRepositoryRequest(BaseModel):
    project_id:     str
    project_name:   str
    owner:          Optional[str] = None   # falls back to settings.github_default_owner
    visibility:     RepositoryVisibility = RepositoryVisibility.PRIVATE
    description:    Optional[str] = None


class CreateBranchRequest(BaseModel):
    project_id:  str
    branch_type: BranchType
    task_id:     Optional[str] = None      # required for feature/fix
    incident_id: Optional[str] = None      # required for hotfix
    slug:        Optional[str] = None      # required for feature/fix/hotfix
    base_branch: Optional[str] = None      # defaults to "develop" (or "main" for hotfix)


class CommitFilesRequest(BaseModel):
    project_id: str
    branch_name: str
    message:     str
    files:       List[FileChange]
    metadata:    CommitMetadata


class CreatePullRequestRequest(BaseModel):
    project_id:    str
    source_branch: str
    target_branch: Optional[str] = None    # defaults to "develop"
    title:         str
    description:   Optional[str] = None
    task_id:       Optional[str] = None
    reviewers:     List[str] = Field(default_factory=list)


class ApprovePullRequestRequest(BaseModel):
    project_id: str
    pull_request_id: str
    approved_by: str


class MergePullRequestRequest(BaseModel):
    project_id: str
    pull_request_id: str


class CreateReleaseRequest(BaseModel):
    project_id:  str
    tag_name:    str
    target_branch: Optional[str] = None    # defaults to repository default_branch
    name:        Optional[str] = None
    body:        Optional[str] = None
    prerelease:  bool = False


class RollbackReleaseRequest(BaseModel):
    project_id: str
    tag_name:   str
    reason:     str


# ── Responses ─────────────────────────────────────────────────────

class RepositoryResponse(BaseModel):
    id:             str
    project_id:     str
    provider:       str
    owner:          str
    name:           str
    full_name:      str
    default_branch: str
    clone_url:      Optional[str] = None
    html_url:       Optional[str] = None
    visibility:     str
    status:         str
    created_at:     datetime

    model_config = {"from_attributes": True}


class BranchResponse(BaseModel):
    id:            str
    repository_id: str
    name:          str
    branch_type:   str
    task_id:       Optional[str] = None
    base_branch:   str
    head_sha:      Optional[str] = None
    is_protected:  bool
    status:        str
    created_at:    datetime

    model_config = {"from_attributes": True}


class CommitResponse(BaseModel):
    sha:         str
    message:     str
    branch_name: str
    html_url:    Optional[str] = None


class PullRequestResponse(BaseModel):
    id:                 str
    repository_id:      str
    provider_pr_number: Optional[int] = None
    title:              str
    description:        Optional[str] = None
    source_branch:      str
    target_branch:      str
    status:             str
    merge_strategy:      str
    reviewers:          List[str]
    merge_sha:          Optional[str] = None
    html_url:           Optional[str] = None
    opened_at:          datetime

    model_config = {"from_attributes": True}


class ReleaseResponse(BaseModel):
    tag_name:  str
    name:      Optional[str] = None
    html_url:  Optional[str] = None
    target_sha: Optional[str] = None
    prerelease: bool = False


class RepositoryEventResponse(BaseModel):
    id:          str
    event_type:  str
    entity_type: Optional[str] = None
    entity_id:   Optional[str] = None
    actor:       str
    payload:     Dict[str, Any]
    recorded_at: datetime

    model_config = {"from_attributes": True}


# ── Provider-level result types (transport-agnostic) ─────────────

class ProviderRepoResult(BaseModel):
    provider_repo_id: str
    owner:            str
    name:             str
    full_name:        str
    default_branch:   str
    clone_url:        str
    html_url:         str
    visibility:       str


class ProviderBranchResult(BaseModel):
    name:     str
    head_sha: str


class ProviderCommitResult(BaseModel):
    sha:      str
    html_url: Optional[str] = None


class ProviderPullRequestResult(BaseModel):
    number:   int
    html_url: str
    state:    str
    mergeable: Optional[bool] = None


class ProviderReleaseResult(BaseModel):
    tag_name:   str
    html_url:   str
    target_sha: str


# ── Errors ─────────────────────────────────────────────────────

class RepositoryServiceError(Exception):
    """Base class for all Repository Service errors."""


class ProviderUnavailableError(RepositoryServiceError):
    """Raised when the SCM provider (e.g. GitHub) cannot be reached."""


class InvalidBranchNameError(RepositoryServiceError):
    """Raised when a branch name violates the naming policy."""


class ProtectedBranchViolationError(RepositoryServiceError):
    """Raised on any attempt to force-push, delete, or bypass a protected branch."""


class MergeConflictError(RepositoryServiceError):
    """Raised when a pull request cannot be merged due to a conflict."""


class DuplicateTagError(RepositoryServiceError):
    """Raised when a release tag already exists."""


class PermissionDeniedError(RepositoryServiceError):
    """Raised when the provider rejects an operation for permission reasons."""
