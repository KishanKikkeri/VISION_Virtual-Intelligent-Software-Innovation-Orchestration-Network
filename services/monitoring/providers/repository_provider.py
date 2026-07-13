"""
services/monitoring/providers/repository_provider.py
=======================================================================
Read-only view into Repository Service's own tables (`pull_requests`).
Per spec §6, Monitoring never writes here — SELECT only, via the
shared `db_factory` every BaseAgent already has injected.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, List

from sqlalchemy import select

from services.monitoring.models import MonitoredComponent
from services.monitoring.providers.base import MetricsProvider

# A PR open longer than this without merging/closing counts against the score.
PR_STUCK_SLA_HOURS = 48


class RepositoryProvider(MetricsProvider):
    component = MonitoredComponent.REPOSITORY

    def __init__(self, db_factory: Any):
        self._db_factory = db_factory

    async def collect(self) -> List:
        if self._db_factory is None:
            return self._degraded("db_factory not configured")
        try:
            from infrastructure.database.models import PullRequest

            cutoff = datetime.utcnow() - timedelta(hours=PR_STUCK_SLA_HOURS)
            async with self._db_factory() as db:
                open_result = await db.execute(
                    select(PullRequest).where(PullRequest.status == "open"))
                open_prs = list(open_result.scalars().all())

            if not open_prs:
                return self._healthy("repository_pr_sla", 100.0, open_prs="0")

            stuck = [p for p in open_prs if p.opened_at and p.opened_at < cutoff]
            ratio = 1.0 - (len(stuck) / len(open_prs))
            return self._healthy(
                "repository_pr_sla", round(ratio * 100.0, 2),
                open_prs=str(len(open_prs)), stuck_prs=str(len(stuck)),
            )
        except Exception as e:
            return self._degraded(str(e))
