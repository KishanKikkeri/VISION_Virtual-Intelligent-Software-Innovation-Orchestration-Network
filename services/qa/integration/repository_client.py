"""
services/qa/integration/repository_client.py
================================================
QA's read-only view of Repository Service. Per the spec:

    QA has read-only access to Repository Service.
    Allowed:   Read commits, Read branches, Read pull requests, Read releases
    Forbidden: Commit, Merge, Push, Create branches, Modify repository state

This client exposes ONLY GET calls against Repository Service's REST API
(services/repository/api/routes.py) — there is no method here capable of
issuing a POST/PUT/DELETE. That is a deliberate, enforced asymmetry with
services/engineering/integration/repository_client.py (which is the only
write path in the whole platform).

Design note (docs/M3.4_QA_Service_Handover.md): Repository Service does
not expose a dedicated "list commits" GET endpoint (commits are created
via POST /commits and surfaced through branch/PR/event history rather
than a standalone collection). `get_commit_history` therefore reads the
release/audit event stream and pull-request list rather than a
nonexistent endpoint — documented here rather than silently assumed.
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


class QARepositoryReadClient:
    """Read-only async HTTP wrapper around Repository Service's public API."""

    def __init__(self, base_url: Optional[str] = None, timeout: float = 30.0):
        settings = get_settings()
        self._base_url = base_url or f"http://localhost:{settings.repository_service_port}"
        self._timeout = timeout

    async def _get(self, path: str) -> Any:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            resp = await client.get(path)
        if resp.status_code >= 400:
            log.warning("qa_repository_read_failed", path=path, status=resp.status_code)
            raise RepositoryServiceClientError(path, resp.status_code, resp.text)
        return resp.json()

    # ── The four read-only surfaces QA is permitted to consume ────

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
