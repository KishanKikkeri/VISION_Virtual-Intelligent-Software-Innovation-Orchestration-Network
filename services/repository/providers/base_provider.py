"""
services/repository/providers/base_provider.py
=================================================
BaseRepositoryProvider — the adapter interface every SCM provider must
implement. Adding GitLab, Bitbucket, or Azure DevOps support means
writing a new subclass of this and registering it in the provider
factory. Nothing above this layer (managers, workflows, API) may ever
import a provider-specific SDK or shape its logic around one provider's
quirks — that defeats the purpose of the abstraction.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from services.repository.schemas import (
    FileChange,
    ProviderBranchResult,
    ProviderCommitResult,
    ProviderPullRequestResult,
    ProviderReleaseResult,
    ProviderRepoResult,
)


class BaseRepositoryProvider(ABC):
    """
    Every method is async and raises a RepositoryServiceError subclass
    (see services.repository.schemas) on failure — never a raw
    provider-SDK exception. Managers depend only on these contracts.
    """

    name: str  # e.g. "github", "gitlab"

    # ── Repository lifecycle ───────────────────────────────────

    @abstractmethod
    async def create_repository(
        self,
        owner: str,
        name: str,
        description: Optional[str],
        visibility: str,
    ) -> ProviderRepoResult: ...

    @abstractmethod
    async def get_repository(self, owner: str, name: str) -> ProviderRepoResult: ...

    # ── Branch lifecycle ────────────────────────────────────────

    @abstractmethod
    async def create_branch(
        self,
        owner: str,
        repo: str,
        branch_name: str,
        base_branch: str,
    ) -> ProviderBranchResult: ...

    @abstractmethod
    async def get_branch(
        self, owner: str, repo: str, branch_name: str,
    ) -> ProviderBranchResult: ...

    @abstractmethod
    async def delete_branch(self, owner: str, repo: str, branch_name: str) -> None: ...

    # ── Commit lifecycle ────────────────────────────────────────

    @abstractmethod
    async def commit_files(
        self,
        owner: str,
        repo: str,
        branch_name: str,
        message: str,
        files: List[FileChange],
        author_name: str,
        author_email: str,
    ) -> ProviderCommitResult: ...

    # ── Pull request lifecycle ───────────────────────────────────

    @abstractmethod
    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        body: Optional[str],
        head: str,
        base: str,
        reviewers: List[str],
    ) -> ProviderPullRequestResult: ...

    @abstractmethod
    async def get_pull_request(
        self, owner: str, repo: str, number: int,
    ) -> ProviderPullRequestResult: ...

    @abstractmethod
    async def merge_pull_request(
        self, owner: str, repo: str, number: int, commit_message: str,
    ) -> str:
        """Squash-merges the PR. Returns the merge commit SHA."""
        ...

    # ── Release lifecycle ────────────────────────────────────────

    @abstractmethod
    async def create_release(
        self,
        owner: str,
        repo: str,
        tag_name: str,
        target_commitish: str,
        name: Optional[str],
        body: Optional[str],
        prerelease: bool,
    ) -> ProviderReleaseResult: ...

    @abstractmethod
    async def delete_release(self, owner: str, repo: str, tag_name: str) -> None: ...
