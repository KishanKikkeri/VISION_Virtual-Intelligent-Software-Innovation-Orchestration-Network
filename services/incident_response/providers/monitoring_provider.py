"""
services/incident_response/providers/monitoring_provider.py
=======================================================================
Read-only view into Monitoring Service's own tables (`alerts`,
`system_health`). Per the handover's §9 Repository Pattern, Incident
Response never writes into Monitoring's tables — SELECT only, via the
shared `db_factory` every BaseAgent already has injected.
"""
from __future__ import annotations

from typing import Any, List

from sqlalchemy import select

from services.incident_response.models import EvidenceItem, EvidenceSource
from services.incident_response.providers.base import EvidenceProvider

RECENT_ALERTS_WINDOW = 10


class MonitoringProvider(EvidenceProvider):
    source = EvidenceSource.MONITORING.value

    def __init__(self, db_factory: Any):
        self._db_factory = db_factory

    async def collect(self, component: str) -> List[EvidenceItem]:
        if self._db_factory is None:
            return self._empty()
        try:
            from infrastructure.database.models import Alert, SystemHealth

            async with self._db_factory() as db:
                alert_result = await db.execute(
                    select(Alert).where(Alert.component == component)
                    .order_by(Alert.last_seen_at.desc()).limit(RECENT_ALERTS_WINDOW))
                alerts = list(alert_result.scalars().all())

                health_result = await db.execute(
                    select(SystemHealth).order_by(SystemHealth.cycle_at.desc()).limit(1))
                latest_health = health_result.scalar_one_or_none()

            items: List[EvidenceItem] = []
            for alert in alerts:
                items.append(EvidenceItem(
                    source=EvidenceSource.MONITORING, ref=alert.id,
                    summary=f"alert[{alert.severity}] {alert.status}: {alert.message}",
                ))
            if latest_health is not None:
                items.append(EvidenceItem(
                    source=EvidenceSource.MONITORING, ref=latest_health.id,
                    summary=f"latest system_health={latest_health.health_score} "
                            f"({latest_health.status}) at {latest_health.cycle_at.isoformat()}",
                ))
            return items
        except Exception:
            return self._empty()
