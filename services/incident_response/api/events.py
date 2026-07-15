"""
services/incident_response/api/events.py
=================================
NATS event bindings for Incident Response Service, per spec §11.

Subscribes:
  monitoring.incident   — the ONLY event that opens a new incident
                          (spec §4 "receive incident_candidate"). Each
                          message runs one full W-INCIDENT-RESPONSE
                          invocation (services/incident_response/
                          workflows/incident_response_graph.py) as a
                          background task, keyed by incident_id as the
                          checkpointer thread_id.
  monitoring.alert
  monitoring.warning    — recorded for cross-reference only (not a
                          lifecycle trigger — spec §3 "Monitoring
                          already established ... alert deduplication.
                          Incident Response must consume these", not
                          redesign them). No W-INCIDENT-RESPONSE
                          invocation follows from these; Monitoring
                          itself decides when a warning/alert has
                          become a full incident_candidate.

Publishes (queued by IncidentResponseHead's AgentResult / the graph's
finalize node):
  incident.created  incident.updated  incident.resolved
  incident.rollback.requested  incident.phase.completed
  incident.notification (see providers/notification_provider.py)
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import structlog

from core.runtime.factory import AgentFactory
from infrastructure.messaging.nats_client import NATSClient
from services.incident_response.workflows.incident_response_graph import (
    build_incident_response_graph,
    initial_state,
)

log = structlog.get_logger(__name__)


async def setup_incident_response_subscriptions(
    nats: NATSClient, factory: AgentFactory, project_id: str, db_factory=None,
) -> None:
    """Called once from main.py's startup lifespan."""

    graph = build_incident_response_graph(factory)

    async def _run_incident(payload: Dict[str, Any]) -> None:
        incident_id = payload.get("incident_id")
        component = payload.get("component")
        severity = payload.get("severity", "critical")
        breach_cycles = int(payload.get("breach_cycles", 1))
        if not incident_id or not component:
            log.warning("incident_response_incident_payload_incomplete", payload=payload)
            return

        state = initial_state(
            incident_id=incident_id, component=component, severity=severity,
            project_id=project_id, breach_cycles=breach_cycles,
        )
        config = {"configurable": {"thread_id": f"incident-{incident_id}"}}
        try:
            final_state = await graph.ainvoke(state, config=config)
            log.info("incident_response_lifecycle_completed", incident_id=incident_id,
                     final_status=final_state.get("final_status"))
        except Exception as e:
            log.error("incident_response_lifecycle_failed", incident_id=incident_id,
                      error=str(e), exc_info=True)

    async def incident_handler(payload: Dict[str, Any]) -> None:
        # Fire-and-forget: don't block the NATS consumer/ack on a full
        # incident lifecycle (which may include an HTTP rollback call).
        asyncio.create_task(_run_incident(payload))

    async def _record_visibility(level: str, subject: str, payload: Dict[str, Any]) -> None:
        if db_factory is None:
            return
        try:
            from services.monitoring.integration.monitoring_repository import MonitoringLogRepository
            async with db_factory() as db:
                await MonitoringLogRepository.record(db, "incident_response", level, subject, payload)
        except Exception as e:
            log.warning("incident_response_event_record_failed", error=str(e))

    async def alert_handler(payload: Dict[str, Any]) -> None:
        await _record_visibility("info", "monitoring.alert", payload)

    async def warning_handler(payload: Dict[str, Any]) -> None:
        await _record_visibility("debug", "monitoring.warning", payload)

    await nats.subscribe("monitoring.incident", incident_handler, durable="incident-response-incident")
    await nats.subscribe("monitoring.alert", alert_handler, durable="incident-response-alert")
    await nats.subscribe("monitoring.warning", warning_handler, durable="incident-response-warning")

    log.info("incident_response_subscriptions_ready")
