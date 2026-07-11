"""
services/engineering/integration/repository_client.py
========================================================
The ONLY code path in the Engineering service allowed to reach
Repository Service. Per the spec:

    Engineering must never: call Git, write commits directly,
    create branches directly.
    Engineering calls: create_branch() commit_files()
    create_pull_request() approve_pull_request() merge_pull_request()

All calls go over HTTP to Repository Service's REST API
(services/repository/api/routes.py) — never a git binary, never a
provider SDK, never direct DB access.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx
import structlog

from core.config.settings import get_settings

log = structlog.get_logger(__name__)


class RepositoryServiceClientError(Exception):
    """Raised on any non-2xx response from Repository Service."""

    def __init__(self, path: str, status_code: int, detail: str):
        self.path = path
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Repository Service {path} -> {status_code}: {detail}")


class RepositoryServiceClient:
    """Thin async HTTP wrapper around Repository Service's public API."""

    def __init__(self, base_url: Optional[str] = None, timeout: float = 30.0):
        settings = get_settings()
        self._base_url = base_url or f"http://localhost:{settings.repository_service_port}"
        self._timeout = timeout

    async def _request(self, method: str, path: str, json: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            resp = await client.request(method, path, json=json)
        if resp.status_code >= 400:
            log.warning("repository_service_call_failed", path=path, status=resp.status_code)
            raise RepositoryServiceClientError(path, resp.status_code, resp.text)
        return resp.json()

    # ── The five calls Engineering is permitted to make ──────────

    async def create_integration_branch(
        self, project_id: str, feature_name: str, base_branch: str = "develop",
    ) -> Dict[str, Any]:
        """
        Creates `integration/<feature-name>` via the Appendix A branch_type.
        Owned by the Engineering Lead, per the Repository Patch.
        """
        return await self._request("POST", "/branches", {
            "project_id": project_id,
            "branch_type": "integration",
            "slug": feature_name,
            "base_branch": base_branch,
        })

    async def commit_files(
        self,
        project_id: str,
        branch_name: str,
        message: str,
        files: List[Dict[str, str]],
        metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Replays a worker's files onto the integration branch. Never a merge
        commit — Repository Service enforces that server-side.
        """
        return await self._request("POST", "/commits", {
            "project_id": project_id,
            "branch_name": branch_name,
            "message": message,
            "files": files,
            "metadata": metadata,
        })

    async def create_pull_request(
        self,
        project_id: str,
        source_branch: str,
        title: str,
        description: Optional[str] = None,
        target_branch: Optional[str] = None,
        task_id: Optional[str] = None,
        reviewers: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        return await self._request("POST", "/pull-requests", {
            "project_id": project_id,
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
            "task_id": task_id,
            "reviewers": reviewers or [],
        })

    async def approve_pull_request(
        self, project_id: str, pull_request_id: str, approved_by: str,
    ) -> Dict[str, Any]:
        return await self._request("POST", "/pull-requests/approve", {
            "project_id": project_id,
            "pull_request_id": pull_request_id,
            "approved_by": approved_by,
        })

    async def merge_pull_request(self, project_id: str, pull_request_id: str) -> Dict[str, Any]:
        """Always a squash merge — Repository Service forbids merge commits."""
        return await self._request("POST", "/pull-requests/merge", {
            "project_id": project_id,
            "pull_request_id": pull_request_id,
        })
