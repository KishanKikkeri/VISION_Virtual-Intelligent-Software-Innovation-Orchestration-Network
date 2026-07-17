"""
services/monitoring/api/events.py
=================================
NATS event bindings for Monitoring Service, per spec §10.

Subscribes (messaging cleanup, see docs/M3.9_Platform_Integration_
Handover.md §6 and the follow-up cleanup pass): a `.>` wildcard per
department, consumed purely for cross-department visibility — this
single mechanism now covers every `*.phase.completed`/`*.phase.
started`/`*.phase.failed` event across Architecture/Engineering/QA/
Security/DevOps, QA's `qa.defect.created`/`qa.retry.requested`,
Security's `security.retry.requested`, and Incident Response's own
`incident.*` events, all of which the M3.9 platform-integration audit
found had zero consumers. (Those events don't drive any control flow —
Manager's own `services/manager/graphs/lifecycle.py::engineering_rework_node`
already handles the QA/Security rework loop synchronously from the
delegation result, not by consuming these NATS events — so this
wildcard subscription is the *correct* fix, not a workaround: these
events are genuinely fire-and-forget observability signals, and now
they're actually observed.)

  architecture.>  engineering.>  qa.>  security.>  devops.>  incident.>
  repository.>     manager.>       agent.>  system.>

None of these drive Monitoring's own cycle (that's the scheduler in
main.py, spec §7) — they're recorded as `logs` rows so a failed phase
is visible on the platform_overview dashboard between cycles, and so
the Incident Response Service has a single place
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
# visibility — one `.>` wildcard per department (plus the original
# infrastructure-level repository/manager/agent/system prefixes).
# Monitoring intentionally does NOT subscribe to its own "monitoring.>"
# — no service needs to observe itself this way.
_WILDCARD_SUBJECTS = (
    "architecture.>", "engineering.>", "qa.>", "security.>", "devops.>", "incident.>",
    "repository.>", "manager.>", "agent.>", "system.>",
)


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

    async def wildcard_handler(subject: str, payload: Dict[str, Any]) -> None:
        department = subject.split(".")[0]
        if subject.endswith(".phase.completed") or subject.endswith(".phase.failed"):
            passed = payload.get("passed", not subject.endswith(".phase.failed"))
            level = "info" if passed else "warning"
        else:
            level = "debug"
        await _record(department, level, subject, payload)

    for prefix in _WILDCARD_SUBJECTS:
        async def _handler(payload: Dict[str, Any], _prefix: str = prefix) -> None:
            await wildcard_handler(_prefix, payload)
        await nats.subscribe(prefix, _handler, durable=f"monitoring-{prefix.replace('.', '-').replace('>', 'all')}")

    log.info("monitoring_subscriptions_ready", wildcard_count=len(_WILDCARD_SUBJECTS))
