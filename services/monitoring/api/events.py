"""
services/monitoring/api/events.py
=================================
NATS event bindings for Monitoring Service, per spec §10.

Subscribes:
  engineering.phase.completed  qa.phase.completed
  security.phase.completed     devops.phase.completed
  repository.>  manager.>  agent.>  system.>

None of these drive Monitoring's own cycle (that's the scheduler in
main.py, spec §7) — they're recorded as `logs` rows so a failed phase
is visible on the platform_overview dashboard between cycles, and so a
future M3.8 Incident Response Service has a single place
(`services/monitoring/integration/monitoring_repository.py`'s
MonitoringLogRepository) to look for "what else was happening around
the time of this incident."

Publishes (queued by MonitoringHead's AgentResult / the graph's
publish_node, flushed via BaseAgent._post_execute / the scheduler):
  monitoring.metrics.updated  monitoring.alert  monitoring.warning
  monitoring.incident         monitoring.phase.completed
"""
from __future__ import annotations

from typing import Any, Dict

import structlog

from infrastructure.messaging.nats_client import NATSClient

log = structlog.get_logger(__name__)

# Wide, low-cardinality subjects consumed just for cross-department
# visibility — one shared handler per phase.completed style, one
# catch-all for each `.>` prefix.
_PHASE_SUBJECTS = (
    "engineering.phase.completed", "qa.phase.completed",
    "security.phase.completed", "devops.phase.completed",
)
_WILDCARD_SUBJECTS = ("repository.>", "manager.>", "agent.>", "system.>")


async def setup_monitoring_subscriptions(nats: NATSClient, db_factory=None) -> None:
    """Called once from main.py's startup lifespan."""

    async def _record(service: str, level: str, message: str, context: Dict[str, Any]) -> None:
        if db_factory is None:
            return
        try:
            from services.monitoring.integration.monitoring_repository import MonitoringLogRepository
            async with db_factory() as db:
                await MonitoringLogRepository.record(db, service, level, message, context)
        except Exception as e:
            log.warning("monitoring_event_record_failed", error=str(e))

    async def phase_completed_handler(subject: str, payload: Dict[str, Any]) -> None:
        department = subject.split(".")[0]
        passed = payload.get("passed", True)
        await _record(
            department, "info" if passed else "warning",
            f"{department}.phase.completed (passed={passed})", payload,
        )

    async def wildcard_handler(subject: str, payload: Dict[str, Any]) -> None:
        department = subject.split(".")[0]
        await _record(department, "debug", subject, payload)

    for subject in _PHASE_SUBJECTS:
        async def _handler(payload: Dict[str, Any], _subject: str = subject) -> None:
            await phase_completed_handler(_subject, payload)
        await nats.subscribe(subject, _handler, durable=f"monitoring-{subject.replace('.', '-')}")

    for prefix in _WILDCARD_SUBJECTS:
        async def _handler(payload: Dict[str, Any], _prefix: str = prefix) -> None:
            await wildcard_handler(_prefix, payload)
        await nats.subscribe(prefix, _handler, durable=f"monitoring-{prefix.replace('.', '-').replace('>', 'all')}")

    log.info("monitoring_subscriptions_ready")
