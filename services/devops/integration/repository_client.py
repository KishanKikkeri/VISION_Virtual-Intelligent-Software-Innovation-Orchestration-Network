"""
services/devops/integration/repository_client.py
=====================================================
DevOps's view of Repository Service — read access plus release/tag
creation, per the spec's Repository Permissions section:

    Allowed: Read, Release creation, Tag creation, Deployment metadata
    Forbidden: Modify source code (Engineering remains sole producer)

This is deliberately a *wider* client than QA's/Security's read-only
ones (services/qa/integration/repository_client.py,
services/security/integration/repository_client.py) — DevOps is the
first service in the pipeline actually permitted to write to
Repository Service, but strictly limited to the release/tag surface
Repository Service already exposes (`POST /releases`,
`POST /releases/rollback`). There is no method here capable of
committing, pushing, merging, or creating a branch — those remain
Engineering-only.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx
import structlog

from core.config.settings import get_settings

log = structlog.get_logger(__name__)


class RepositoryServiceClientError(Exception):
    def __init__(self, path: str, status_code: int, detail: str):
        self.path = path
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Repository Service {path} -> {status_code}: {detail}")


class DevOpsRepositoryClient:
    """Read + release/tag-only async HTTP wrapper around Repository Service."""

    def __init__(self, base_url: Optional[str] = None, timeout: float = 30.0):
        settings = get_settings()
        self._base_url = base_url or f"http://localhost:{settings.repository_service_port}"
        self._timeout = timeout

    async def _request(self, method: str, path: str, json_body: Optional[Dict[str, Any]] = None) -> Any:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            resp = await client.request(method, path, json=json_body)
        if resp.status_code >= 400:
            log.warning("devops_repository_request_failed", path=path, status=resp.status_code)
            raise RepositoryServiceClientError(path, resp.status_code, resp.text)
        return resp.json()

    # -- Read surfaces (same shape as QA's / Security's clients) --

    async def get_repository(self, project_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/repositories/{project_id}")

    async def list_branches(self, project_id: str) -> List[Dict[str, Any]]:
        return await self._request("GET", f"/branches/{project_id}")

    async def list_pull_requests(self, project_id: str) -> List[Dict[str, Any]]:
        return await self._request("GET", f"/pull-requests/{project_id}")

    async def get_release_history(self, project_id: str) -> List[Dict[str, Any]]:
        return await self._request("GET", f"/releases/{project_id}/history")

    # -- Write surfaces — release/tag ONLY, never source code --

    async def create_release(self, project_id: str, tag_name: str,
                              name: Optional[str] = None, body: Optional[str] = None,
                              target_branch: Optional[str] = None,
                              prerelease: bool = False) -> Dict[str, Any]:
        return await self._request("POST", "/releases", {
            "project_id": project_id, "tag_name": tag_name, "name": name,
            "body": body, "target_branch": target_branch, "prerelease": prerelease,
        })

    async def rollback_release(self, project_id: str, tag_name: str, reason: str) -> Dict[str, Any]:
        return await self._request("POST", "/releases/rollback", {
            "project_id": project_id, "tag_name": tag_name, "reason": reason,
        })
