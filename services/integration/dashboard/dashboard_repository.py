"""
services/integration/dashboard/dashboard_repository.py
=================================
M4.3 §2 Dashboard Repository — the "repository wrapper" layer
(architectural decision #3). Turns ORM rows into the plain dicts
`dashboard_builder.py`'s pure functions consume, same division of
labor `execution_timeline.get_execution_timeline` and
`artifact_diff.diff_artifact_versions` already use for their own pure
cores.

Two read paths live here:

  - `get_recent_events` — a thin wrapper over the new
    `AuditRepository.list_recent` (added alongside this module; see
    infrastructure/database/repositories/__init__.py), for the Event
    Stream card.

  - `get_incidents` — **a documented fallback, not a claim that this
    is "the" incident data source.** M3.6 (Incident Response,
    completed per the handover) almost certainly already has its own
    incident table/repository elsewhere in this codebase; this module
    doesn't have visibility into that department's models from where
    M4.3 was implemented. Rather than invent a duplicate incident
    concept, `get_incidents` derives a best-effort incident list from
    the same audit trail (rows whose event_type's first dot-segment,
    per `dashboard_builder.categorize_event_type`, is "incident"),
    which is honest about being a projection of *audit records about*
    incidents, not the incident system of record. The moment a real
    `IncidentRepository` (or equivalent) is confirmed, swap the one
    line in `get_incidents` for a real query — `dashboard_builder.
    build_incident_list`'s input contract (a list of plain dicts) does
    not need to change.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _event_row_to_dict(row: Any) -> Dict[str, Any]:
    recorded_at = getattr(row, "recorded_at", None)
    return {
        "id": getattr(row, "id", None),
        "event_type": row.event_type,
        "actor_type": row.actor_type,
        "actor_id": row.actor_id,
        "project_id": getattr(row, "project_id", None),
        "entity_type": getattr(row, "entity_type", None),
        "entity_id": getattr(row, "entity_id", None),
        "payload": dict(row.payload or {}) if getattr(row, "payload", None) else {},
        "recorded_at": recorded_at.isoformat() if hasattr(recorded_at, "isoformat") else recorded_at,
    }


async def get_recent_events(db: Any, limit: int = 200, category: Optional[str] = None) -> List[Dict[str, Any]]:
    """Most-recent-first, platform-wide. `category` (if given) is
    pushed down to the DB as an event_type-prefix filter via
    `AuditRepository.list_recent` rather than filtered in Python."""
    from infrastructure.database.repositories import AuditRepository  # local import: avoid import cycles

    rows = await AuditRepository.list_recent(db, limit=limit, event_type_prefix=category)
    return [_event_row_to_dict(r) for r in rows]


async def get_incidents(db: Any, limit: int = 100) -> List[Dict[str, Any]]:
    """See module docstring — audit-trail-derived fallback pending a
    confirmed dedicated incident repository. Only rows categorized
    "incident" (event_type starting `incident.`) are surfaced; each is
    marked `source: "audit_trail"` so a caller/UI can tell this apart
    from a future `source: "incident_service"` row without a schema
    change."""
    events = await get_recent_events(db, limit=limit, category="incident")
    incidents: List[Dict[str, Any]] = []
    for e in events:
        payload = e.get("payload") or {}
        incidents.append({
            "id": str(e.get("id") or e.get("entity_id") or e.get("recorded_at")),
            "title": payload.get("title") or e.get("event_type"),
            "severity": payload.get("severity", "warning"),
            "status": payload.get("status", "open"),
            "workflow": payload.get("workflow"),
            "opened_at": e.get("recorded_at"),
            "source": "audit_trail",
        })
    return incidents
