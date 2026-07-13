"""
services/security/integration/repository_client.py
=======================================================
Security's read-only view of Repository Service. Per the spec:

    Security has read-only access to Repository Service.
    Forbidden: commit, push, merge, create branches, modify repositories.

This client exposes ONLY GET calls against Repository Service's REST API
(services/repository/api/routes.py) — there is no method here capable of
issuing a POST/PUT/DELETE. Identical asymmetry to
services/qa/integration/repository_client.py's QARepositoryReadClient;
duplicated (rather than imported) so Security's read-only guarantee does
not depend on QA's module staying unmodified — each service owns its
own client, per the spec's isolation principle ("Security validates; it
never edits code, commits changes, or performs repository operations
beyond read access").

Design note (docs/M3.5_Security_Service_Handover.md): Repository Service
does not expose a dedicated "list commits" GET endpoint (commits are
created via POST /commits and surfaced through branch/PR/event history
rather than a standalone collection) — same gap M3.4 documented.
`get_commit_history` therefore reads the release/audit event stream
rather than a nonexistent endpoint.
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


class SecurityRepositoryReadClient:
    """Read-only async HTTP wrapper around Repository Service's public API."""

    def __init__(self, base_url: Optional[str] = None, timeout: float = 30.0):
        settings = get_settings()
        self._base_url = base_url or f"http://localhost:{settings.repository_service_port}"
        self._timeout = timeout

    async def _get(self, path: str) -> Any:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            resp = await client.get(path)
        if resp.status_code >= 400:
            log.warning("security_repository_read_failed", path=path, status=resp.status_code)
            raise RepositoryServiceClientError(path, resp.status_code, resp.text)
        return resp.json()

    # ── The read-only surfaces Security is permitted to consume ────

    async def get_repository(self, project_id: str) -> Dict[str, Any]:
        return await self._get(f"/repositories/{project_id}")

    async def list_branches(self, project_id: str) -> List[Dict[str, Any]]:
        return await self._get(f"/branches/{project_id}")

    async def list_pull_requests(self, project_id: str) -> List[Dict[str, Any]]:
        return await self._get(f"/pull-requests/{project_id}")

    async def get_release_history(self, project_id: str) -> List[Dict[str, Any]]:
        return await self._get(f"/releases/{project_id}/history")

    async def get_commit_history(self, repository_id: str) -> List[Dict[str, Any]]:
        """
        Reads commit-adjacent audit events for a repository. See module
        docstring: Repository Service has no standalone GET /commits
        collection, so this surfaces commit activity via the generic
        event log instead.
        """
        events = await self._get(f"/events/{repository_id}")
        return [e for e in events if "commit" in str(e.get("event_type", "")).lower()]
