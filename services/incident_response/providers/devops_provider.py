"""
services/incident_response/providers/devops_provider.py
=======================================================================
Two responsibilities, both about DevOps Service, kept in one provider
per the handover's example list (§8):

  collect()          — read-only view into DevOps's own `deployments`
                        table (SELECT only, shared db_factory), used to
                        both build evidence and decide whether a recent
                        deployment correlates with this incident.

  trigger_rollback() — the ONLY way Incident Response ever asks DevOps
                        to roll something back. It calls DevOps's own
                        existing `POST /devops/rollback` HTTP endpoint
                        (services/devops/api/routes.py) rather than
                        writing to DevOps's `rollback_records` table or
                        calling DevOps's internal Python directly — this
                        is the zero-modification integration point: no
                        DevOps file changes for M3.8 at all.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import select

from services.incident_response.models import EvidenceItem, EvidenceSource
from services.incident_response.providers.base import EvidenceProvider

# A deployment completed within this window is considered a plausible
# root cause correlated with a CRITICAL incident just detected.
RECENT_DEPLOYMENT_CORRELATION_WINDOW_MINUTES = 120
RECENT_DEPLOYMENTS_LIMIT = 5


class DevOpsProvider(EvidenceProvider):
    source = EvidenceSource.DEVOPS.value

    def __init__(self, db_factory: Any, devops_base_url: Optional[str] = None):
        self._db_factory = db_factory
        self._base_url = devops_base_url or "http://localhost:8010"

    async def collect(self, component: str) -> List[EvidenceItem]:
        if self._db_factory is None:
            return self._empty()
        try:
            from infrastructure.database.models import Deployment

            async with self._db_factory() as db:
                result = await db.execute(
                    select(Deployment).order_by(Deployment.created_at.desc())
                    .limit(RECENT_DEPLOYMENTS_LIMIT))
                recent = list(result.scalars().all())

            return [
                EvidenceItem(
                    source=EvidenceSource.DEVOPS, ref=d.id,
                    summary=f"deployment status={d.status} env={d.environment} "
                            f"version={d.version or 'unknown'} at {d.created_at.isoformat()}",
                )
                for d in recent
            ]
        except Exception:
            return self._empty()

    async def recent_deployment_correlation(self) -> Optional[Dict[str, Any]]:
        """
        Returns {"project_id", "deployment_id", "status", "completed_at"}
        for the most recent deployment completed within the correlation
        window, or None if no deployment correlates. Never raises.
        """
        if self._db_factory is None:
            return None
        try:
            from infrastructure.database.models import Deployment

            cutoff = datetime.utcnow() - timedelta(minutes=RECENT_DEPLOYMENT_CORRELATION_WINDOW_MINUTES)
            async with self._db_factory() as db:
                result = await db.execute(
                    select(Deployment).where(Deployment.completed_at.isnot(None))
                    .order_by(Deployment.completed_at.desc()).limit(1))
                latest = result.scalar_one_or_none()

            if latest is None or latest.completed_at is None or latest.completed_at < cutoff:
                return None
            return {
                "project_id": latest.project_id, "deployment_id": latest.id,
                "status": latest.status, "completed_at": latest.completed_at.isoformat(),
            }
        except Exception:
            return None

    async def trigger_rollback(self, project_id: str, reason: str) -> Dict[str, Any]:
        """
        Calls DevOps's existing POST /devops/rollback. Never raises —
        returns {"status": "unreachable", ...} on any failure so the
        caller can still record the attempt and degrade gracefully.
        """
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self._base_url}/devops/rollback",
                    json={"project_id": project_id, "reason": reason},
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            return {"status": "unreachable", "project_id": project_id, "error": str(e)}
