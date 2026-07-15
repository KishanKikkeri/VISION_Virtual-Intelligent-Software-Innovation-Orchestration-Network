"""
services/incident_response/providers/websocket_provider.py
=======================================================================
Thin wrapper around infrastructure/websocket/manager.py's ws_manager,
kept separate from notification_provider.py per the handover's §8
example list — this one is specifically for real-time incident
timeline/status broadcasts to the platform UI, not stakeholder
notification delivery. Broadcasts are platform-wide (broadcast_system),
matching Monitoring's own precedent (services/monitoring/head/__init__.py
uses ws_manager.broadcast_system for the same reason: incidents, like
health scores, aren't scoped to one project).
"""
from __future__ import annotations

from typing import Any, Dict

import structlog

log = structlog.get_logger(__name__)


class IncidentWebSocketProvider:
    async def broadcast(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Best-effort — never raises."""
        try:
            from infrastructure.websocket.manager import ws_manager
            await ws_manager.broadcast_system(event_type, payload)
        except Exception as e:
            log.warning("incident_broadcast_system_failed", event_type=event_type, error=str(e))
