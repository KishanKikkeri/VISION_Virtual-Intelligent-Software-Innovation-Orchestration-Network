"""
services/integration/replay/execution_timeline.py
=================================
M4.2 §2 Execution Timeline — reconstructs an ordered, annotated
timeline of what happened to a project from `AuditEvent`
(infrastructure.database.models.AuditEvent), the platform's existing
append-only "black box recorder" that `/platform/traces/{project_id}`
already reads from (most-recent-first, for a live dashboard feed). This
module reads the same table in the opposite direction — oldest first —
and adds three things a raw event list doesn't give you:
  - elapsed time since the previous event and since the trace started,
  - a coarse `category` derived from each event's dot-separated
    `event_type` (its first segment — "approval", "pipeline",
    "architecture", etc. — a convention already in use across
    `services/manager/main.py`'s existing `AuditRepository.record`
    calls, not one this module invents),
  - a per-category count summary, so "what phases did this project
    actually go through, and how much happened in each" doesn't require
    a human to read every row.

Pure/testable core (`build_execution_timeline`) takes a plain list of
event-shaped objects (anything with `.event_type`, `.actor_type`,
`.actor_id`, `.entity_type`, `.entity_id`, `.payload`, `.recorded_at`) —
works equally against real `AuditEvent` ORM rows and synthetic
namespaces in tests, same pattern as `graph_diff`/`state_diff`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class TimelineEvent(BaseModel):
    sequence: int
    event_type: str
    category: str
    actor_type: str
    actor_id: str
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    recorded_at: str
    seconds_since_previous: Optional[float] = None
    seconds_since_start: float = 0.0


class ExecutionTimeline(BaseModel):
    project_id: str
    event_count: int
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    total_duration_seconds: float = 0.0
    category_counts: Dict[str, int] = Field(default_factory=dict)
    events: List[TimelineEvent] = Field(default_factory=list)


def _category_of(event_type: str) -> str:
    return event_type.split(".", 1)[0] if event_type else "unknown"


def build_execution_timeline(project_id: str, events: List[Any]) -> ExecutionTimeline:
    """`events` must already be in ascending chronological order (oldest
    first) — this function doesn't sort, so it stays honest about
    reflecting whatever order the caller (or a test) actually gives it,
    rather than silently re-ordering and potentially masking a caller
    bug upstream."""
    if not events:
        return ExecutionTimeline(project_id=project_id, event_count=0)

    timeline_events: List[TimelineEvent] = []
    category_counts: Dict[str, int] = {}
    start_ts: Optional[datetime] = None
    previous_ts: Optional[datetime] = None

    for i, ev in enumerate(events):
        recorded_at = ev.recorded_at
        ts = recorded_at if isinstance(recorded_at, datetime) else datetime.fromisoformat(str(recorded_at))
        if start_ts is None:
            start_ts = ts

        category = _category_of(ev.event_type)
        category_counts[category] = category_counts.get(category, 0) + 1

        timeline_events.append(TimelineEvent(
            sequence=i,
            event_type=ev.event_type,
            category=category,
            actor_type=ev.actor_type,
            actor_id=ev.actor_id,
            entity_type=getattr(ev, "entity_type", None),
            entity_id=getattr(ev, "entity_id", None),
            payload=dict(ev.payload or {}),
            recorded_at=ts.isoformat(),
            seconds_since_previous=(ts - previous_ts).total_seconds() if previous_ts is not None else None,
            seconds_since_start=(ts - start_ts).total_seconds(),
        ))
        previous_ts = ts

    end_ts = previous_ts
    return ExecutionTimeline(
        project_id=project_id,
        event_count=len(timeline_events),
        started_at=start_ts.isoformat() if start_ts else None,
        ended_at=end_ts.isoformat() if end_ts else None,
        total_duration_seconds=(end_ts - start_ts).total_seconds() if start_ts and end_ts else 0.0,
        category_counts=category_counts,
        events=timeline_events,
    )


async def get_execution_timeline(db: Any, project_id: str, limit: int = 1000) -> ExecutionTimeline:
    from infrastructure.database.repositories import AuditRepository  # local import: avoid import cycles

    events = await AuditRepository.list_for_project_ascending(db, project_id, limit=limit)
    return build_execution_timeline(project_id, events)
