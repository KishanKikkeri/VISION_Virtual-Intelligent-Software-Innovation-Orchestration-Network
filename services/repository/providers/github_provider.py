"""
services/repository/providers/github_provider.py
====================================================
GitHub V1 provider. Talks directly to the GitHub REST API over httpx
(no PyGithub dependency — keeps the provider async-native and keeps
the dependency footprint identical to the rest of AASC).

Commits are created atomically via the Git Data API (blob → tree →
commit → ref update) rather than the Contents API, so a multi-file
commit_files() call produces exactly one commit, never one per file.
"""
from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional

import httpx
import structlog

from services.repository.providers.base_provider import BaseRepositoryProvider
from services.repository.schemas import (
    DuplicateTagError,
    FileChange,
    InvalidBranchNameError,
    MergeConflictError,
    PermissionDeniedError,
    ProviderBranchResult,
    ProviderCommitResult,
    ProviderPullRequestResult,
    ProviderRepoResult,
    ProviderReleaseResult,
    ProviderUnavailableError,
)

log = structlog.get_logger(__name__)

VISION_BOT_NAME = "VISION Bot"
VISION_BOT_EMAIL = "vision-bot@users.noreply.github.com"


class GitHubProvider(BaseRepositoryProvider):
    """GitHub REST API v3 (application/vnd.github+json) adapter."""

    name = "github"

    def __init__(self, token: str, base_url: str = "https://api.github.com") -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    @staticmethod
    def _raise_for_status(resp: httpx.Response, context: str) -> None:
        if resp.status_code < 400:
            return
        detail = ""
        try:
            detail = resp.json().get("message", "")
        except Exception:
            detail = resp.text[:300]

        if resp.status_code in (401, 403):
            raise PermissionDeniedError(f"{context}: {detail}")
        if resp.status_code == 409:
            raise MergeConflictError(f"{context}: {detail}")
        if resp.status_code == 422 and "already exists" in detail.lower():
            raise DuplicateTagError(f"{context}: {detail}")
        if resp.status_code in (502, 503, 504):
            raise ProviderUnavailableError(f"{context}: {detail}")
        raise ProviderUnavailableError(f"{context}: HTTP {resp.status_code} — {detail}")

    # ── Repository lifecycle ───────────────────────────────────

    async def create_repository(
        self, owner: str, name: str, description: Optional[str], visibility: str,
    ) -> ProviderRepoResult:
        body: Dict[str, Any] = {
            "name": name,
            "description": description or "",
            "private": visibility != "public",
            "auto_init": True,
        }
        try:
            async with self._client() as client:
                # /orgs/{owner}/repos for orgs; falls back to /user/repos for a
                # personal-account owner (GitHub returns 404 on the org route).
                resp = await client.post(f"/orgs/{owner}/repos", json=body)
                if resp.status_code == 404:
                    resp = await client.post("/user/repos", json=body)
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"create_repository: {exc}") from exc
        self._raise_for_status(resp, "create_repository")
        data = resp.json()
        return ProviderRepoResult(
            provider_repo_id=str(data["id"]),
            owner=data["owner"]["login"],
            name=data["name"],
            full_name=data["full_name"],
            default_branch=data.get("default_branch", "main"),
            clone_url=data["clone_url"],
            html_url=data["html_url"],
            visibility="private" if data.get("private") else "public",
        )

    async def get_repository(self, owner: str, name: str) -> ProviderRepoResult:
        try:
            async with self._client() as client:
                resp = await client.get(f"/repos/{owner}/{name}")
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"get_repository: {exc}") from exc
        self._raise_for_status(resp, "get_repository")
        data = resp.json()
        return ProviderRepoResult(
            provider_repo_id=str(data["id"]),
            owner=data["owner"]["login"],
            name=data["name"],
            full_name=data["full_name"],
            default_branch=data.get("default_branch", "main"),
            clone_url=data["clone_url"],
            html_url=data["html_url"],
            visibility="private" if data.get("private") else "public",
        )

    # ── Branch lifecycle ────────────────────────────────────────

    async def create_branch(
        self, owner: str, repo: str, branch_name: str, base_branch: str,
    ) -> ProviderBranchResult:
        try:
            async with self._client() as client:
                base_ref = await client.get(f"/repos/{owner}/{repo}/git/ref/heads/{base_branch}")
                self._raise_for_status(base_ref, "create_branch:get_base_ref")
                base_sha = base_ref.json()["object"]["sha"]

                resp = await client.post(
                    f"/repos/{owner}/{repo}/git/refs",
                    json={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
                )
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"create_branch: {exc}") from exc
        self._raise_for_status(resp, "create_branch")
        data = resp.json()
        return ProviderBranchResult(name=branch_name, head_sha=data["object"]["sha"])

    async def get_branch(self, owner: str, repo: str, branch_name: str) -> ProviderBranchResult:
        try:
            async with self._client() as client:
                resp = await client.get(f"/repos/{owner}/{repo}/git/ref/heads/{branch_name}")
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"get_branch: {exc}") from exc
        self._raise_for_status(resp, "get_branch")
        data = resp.json()
        return ProviderBranchResult(name=branch_name, head_sha=data["object"]["sha"])

    async def delete_branch(self, owner: str, repo: str, branch_name: str) -> None:
        if branch_name in ("main", "develop"):
            raise PermissionDeniedError(
                f"Refusing to delete protected branch '{branch_name}'"
            )
        try:
            async with self._client() as client:
                resp = await client.delete(f"/repos/{owner}/{repo}/git/refs/heads/{branch_name}")
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"delete_branch: {exc}") from exc
        if resp.status_code == 404:
            return  # already gone — idempotent
        self._raise_for_status(resp, "delete_branch")

    # ── Commit lifecycle (Git Data API — atomic multi-file commit) ─

    async def commit_files(
        self,
        owner: str,
        repo: str,
        branch_name: str,
        message: str,
        files: List[FileChange],
        author_name: str = VISION_BOT_NAME,
        author_email: str = VISION_BOT_EMAIL,
    ) -> ProviderCommitResult:
        if not files:
            raise InvalidBranchNameError("commit_files called with an empty file list")
        try:
            async with self._client() as client:
                ref = await client.get(f"/repos/{owner}/{repo}/git/ref/heads/{branch_name}")
                self._raise_for_status(ref, "commit_files:get_ref")
                parent_sha = ref.json()["object"]["sha"]

                parent_commit = await client.get(f"/repos/{owner}/{repo}/git/commits/{parent_sha}")
                self._raise_for_status(parent_commit, "commit_files:get_parent_commit")
                base_tree_sha = parent_commit.json()["tree"]["sha"]

                tree_entries = []
                for f in files:
                    blob = await client.post(
                        f"/repos/{owner}/{repo}/git/blobs",
                        json={
                            "content": base64.b64encode(f.content.encode()).decode(),
                            "encoding": "base64",
                        },
                    )
                    self._raise_for_status(blob, "commit_files:create_blob")
                    tree_entries.append({
                        "path": f.path,
                        "mode": f.mode,
                        "type": "blob",
                        "sha": blob.json()["sha"],
                    })

                tree = await client.post(
                    f"/repos/{owner}/{repo}/git/trees",
                    json={"base_tree": base_tree_sha, "tree": tree_entries},
                )
                self._raise_for_status(tree, "commit_files:create_tree")
                new_tree_sha = tree.json()["sha"]

                commit = await client.post(
                    f"/repos/{owner}/{repo}/git/commits",
                    json={
                        "message": message,
                        "tree": new_tree_sha,
                        "parents": [parent_sha],
                        "author": {"name": author_name, "email": author_email},
                        "committer": {"name": author_name, "email": author_email},
                    },
                )
                self._raise_for_status(commit, "commit_files:create_commit")
                new_commit_sha = commit.json()["sha"]

                update_ref = await client.patch(
                    f"/repos/{owner}/{repo}/git/refs/heads/{branch_name}",
                    json={"sha": new_commit_sha, "force": False},
                )
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"commit_files: {exc}") from exc
        self._raise_for_status(update_ref, "commit_files:update_ref")

        return ProviderCommitResult(
            sha=new_commit_sha,
            html_url=f"https://github.com/{owner}/{repo}/commit/{new_commit_sha}",
        )

    # ── Pull request lifecycle ───────────────────────────────────

    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        body: Optional[str],
        head: str,
        base: str,
        reviewers: List[str],
    ) -> ProviderPullRequestResult:
        try:
            async with self._client() as client:
                resp = await client.post(
                    f"/repos/{owner}/{repo}/pulls",
                    json={"title": title, "body": body or "", "head": head, "base": base},
                )
                self._raise_for_status(resp, "create_pull_request")
                data = resp.json()

                if reviewers:
                    rev_resp = await client.post(
                        f"/repos/{owner}/{repo}/pulls/{data['number']}/requested_reviewers",
                        json={"reviewers": reviewers},
                    )
                    # Reviewer assignment failures are non-fatal — the PR still exists.
                    if rev_resp.status_code >= 400:
                        log.warning("reviewer_assignment_failed",
                                    pr=data["number"], status=rev_resp.status_code)
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"create_pull_request: {exc}") from exc

        return ProviderPullRequestResult(
            number=data["number"], html_url=data["html_url"], state=data["state"],
        )

    async def get_pull_request(self, owner: str, repo: str, number: int) -> ProviderPullRequestResult:
        try:
            async with self._client() as client:
                resp = await client.get(f"/repos/{owner}/{repo}/pulls/{number}")
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"get_pull_request: {exc}") from exc
        self._raise_for_status(resp, "get_pull_request")
        data = resp.json()
        return ProviderPullRequestResult(
            number=data["number"], html_url=data["html_url"], state=data["state"],
            mergeable=data.get("mergeable"),
        )

    async def merge_pull_request(
        self, owner: str, repo: str, number: int, commit_message: str,
    ) -> str:
        try:
            async with self._client() as client:
                resp = await client.put(
                    f"/repos/{owner}/{repo}/pulls/{number}/merge",
                    json={
                        "merge_method": "squash",
                        "commit_title": commit_message.splitlines()[0][:200],
                        "commit_message": commit_message,
                    },
                )
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"merge_pull_request: {exc}") from exc

        if resp.status_code == 405:
            raise MergeConflictError(f"PR #{number} is not mergeable (branch protection or conflict)")
        if resp.status_code == 409:
            raise MergeConflictError(f"PR #{number} has a merge conflict (head sha changed)")
        self._raise_for_status(resp, "merge_pull_request")
        return resp.json()["sha"]

    # ── Release lifecycle ────────────────────────────────────────

    async def create_release(
        self,
        owner: str,
        repo: str,
        tag_name: str,
        target_commitish: str,
        name: Optional[str],
        body: Optional[str],
        prerelease: bool,
    ) -> ProviderReleaseResult:
        try:
            async with self._client() as client:
                existing = await client.get(f"/repos/{owner}/{repo}/releases/tags/{tag_name}")
                if existing.status_code == 200:
                    raise DuplicateTagError(f"Release tag '{tag_name}' already exists")

                resp = await client.post(
                    f"/repos/{owner}/{repo}/releases",
                    json={
                        "tag_name": tag_name,
                        "target_commitish": target_commitish,
                        "name": name or tag_name,
                        "body": body or "",
                        "prerelease": prerelease,
                    },
                )
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"create_release: {exc}") from exc
        self._raise_for_status(resp, "create_release")
        data = resp.json()
        return ProviderReleaseResult(
            tag_name=data["tag_name"], html_url=data["html_url"],
            target_sha=target_commitish,
        )

    async def delete_release(self, owner: str, repo: str, tag_name: str) -> None:
        try:
            async with self._client() as client:
                rel = await client.get(f"/repos/{owner}/{repo}/releases/tags/{tag_name}")
                if rel.status_code == 404:
                    return
                self._raise_for_status(rel, "delete_release:lookup")
                release_id = rel.json()["id"]

                resp = await client.delete(f"/repos/{owner}/{repo}/releases/{release_id}")
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"delete_release: {exc}") from exc
        if resp.status_code not in (204, 404):
            self._raise_for_status(resp, "delete_release")
