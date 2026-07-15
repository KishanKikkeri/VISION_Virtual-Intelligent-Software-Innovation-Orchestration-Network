"""
services/incident_response/providers/repository_provider.py
=======================================================================
Read-only view into Repository Service's own tables (`pull_requests`).
Per the handover's §9 Repository Pattern, Incident Response never
writes here — SELECT only, via the shared `db_factory`.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, List

from sqlalchemy import select

from services.incident_response.models import EvidenceItem, EvidenceSource
from services.incident_response.providers.base import EvidenceProvider

# A PR merged within this window is considered a plausible contributing
# factor for a correlated deployment/repository component incident.
RECENT_MERGE_WINDOW_HOURS = 24
RECENT_PR_LIMIT = 10


class RepositoryProvider(EvidenceProvider):
    source = EvidenceSource.REPOSITORY.value

    def __init__(self, db_factory: Any):
        self._db_factory = db_factory

    async def collect(self, component: str) -> List[EvidenceItem]:
        if self._db_factory is None:
            return self._empty()
        try:
            from infrastructure.database.models import PullRequest

            cutoff = datetime.utcnow() - timedelta(hours=RECENT_MERGE_WINDOW_HOURS)
            async with self._db_factory() as db:
                result = await db.execute(
                    select(PullRequest).where(PullRequest.status == "merged")
                    .order_by(PullRequest.opened_at.desc()).limit(RECENT_PR_LIMIT))
                prs = list(result.scalars().all())

            recent = [p for p in prs if getattr(p, "opened_at", None) and p.opened_at >= cutoff]
            return [
                EvidenceItem(
                    source=EvidenceSource.REPOSITORY, ref=p.id,
                    summary=f"pull_request merged recently (opened_at={p.opened_at.isoformat()})",
                )
                for p in recent
            ]
        except Exception:
            return self._empty()
