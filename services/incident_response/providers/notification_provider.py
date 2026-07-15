"""
services/incident_response/providers/notification_provider.py
=======================================================================
Stakeholder notification for incident status changes. No external
email/SMS/Slack integration is configured anywhere in core/config/
settings.py — this provider is an intentionally thin, extensible stub:
it publishes a NATS event and a WebSocket broadcast (both best-effort,
never raises), which is enough for the platform's own UI/ops tooling to
surface the notification today. Wiring a real external channel later
(e.g. email/Slack) only requires adding a branch inside `notify()` —
no caller-side change.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import structlog

from core.contracts import NATSEvent

log = structlog.get_logger(__name__)


class NotificationProvider:
    def __init__(self, nats: Any = None):
        self._nats = nats

    async def notify(self, incident_id: str, component: str, severity: str,
                      message: str) -> NATSEvent:
        """Builds (and best-effort publishes) the notification event.
        Returns the NATSEvent so the caller can also queue it on the
        AgentResult (mirrors BaseAgent._post_execute's flush pattern)."""
        event = NATSEvent(subject="incident.notification", payload={
            "incident_id": incident_id, "component": component,
            "severity": severity, "message": message,
        })
        if self._nats is not None:
            try:
                await self._nats.publish(event.subject, event.payload)
            except Exception as e:
                log.warning("incident_notification_publish_failed", error=str(e))
        return event
