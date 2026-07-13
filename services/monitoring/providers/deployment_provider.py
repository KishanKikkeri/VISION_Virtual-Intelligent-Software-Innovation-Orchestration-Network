"""
services/monitoring/providers/deployment_provider.py
=======================================================================
Read-only view into DevOps Service's own `deployments` table. Per spec
§6, Monitoring never writes to another department's operational
tables — SELECT only, via the shared `db_factory`.
"""
from __future__ import annotations

from typing import Any, List

from sqlalchemy import select

from services.monitoring.models import MonitoredComponent
from services.monitoring.providers.base import MetricsProvider

# Only the most recent N deployments are considered — an old rollback
# shouldn't permanently depress the score.
RECENT_DEPLOYMENTS_WINDOW = 20


class DeploymentProvider(MetricsProvider):
    component = MonitoredComponent.DEPLOYMENTS

    def __init__(self, db_factory: Any):
        self._db_factory = db_factory

    async def collect(self) -> List:
        if self._db_factory is None:
            return self._degraded("db_factory not configured")
        try:
            from infrastructure.database.models import Deployment

            async with self._db_factory() as db:
                result = await db.execute(
                    select(Deployment).order_by(Deployment.created_at.desc())
                    .limit(RECENT_DEPLOYMENTS_WINDOW))
                recent = list(result.scalars().all())

            if not recent:
                return self._healthy("deployment_success_ratio", 100.0, deployment_count="0")

            healthy = sum(1 for d in recent if d.status == "healthy")
            ratio = healthy / len(recent)
            return self._healthy(
                "deployment_success_ratio", round(ratio * 100.0, 2),
                deployment_count=str(len(recent)), healthy=str(healthy),
            )
        except Exception as e:
            return self._degraded(str(e))
