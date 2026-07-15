"""
services/incident_response/integration/platform_anchor.py
=========================================================
Architectural decision (extends services/monitoring/integration/
platform_anchor.py's precedent, documented there as spec §0 Decision 6):
Incident Response is the platform's *second* cross-project,
continuously-running-adjacent department (event-driven rather than
scheduled, but still with no single project of its own to anchor
TaskInput.project_id / AgentContext.project_id / artifacts.project_id
to).

Rather than idempotently creating a second sentinel user+project (which
would just duplicate Monitoring's exact mechanism for no benefit), M3.8
reuses Monitoring's existing "Platform Monitoring" sentinel project via
a plain import — zero modification to services/monitoring/. Both
departments are platform-wide observability/operations concerns; one
shared anchor project keeps their artifacts/audit trail on the same
project_id, which is a feature (an incident's artifacts sit alongside
the health reports that triggered it), not a coupling risk (Incident
Response does not import anything else from Monitoring's package root
other than this one idempotent bootstrap function and the frozen
MonitoredComponent/AlertSeverity vocabulary in services/incident_response/
models.py, both intentional per the handover's "Incident Response must
consume these [Monitoring artifacts/events]" instruction).
"""
from __future__ import annotations

from services.monitoring.integration.platform_anchor import (
    PLATFORM_PROJECT_NAME,
    PLATFORM_USER_EMAIL,
    ensure_platform_anchor,
)

__all__ = ["PLATFORM_PROJECT_NAME", "PLATFORM_USER_EMAIL", "ensure_platform_anchor"]
