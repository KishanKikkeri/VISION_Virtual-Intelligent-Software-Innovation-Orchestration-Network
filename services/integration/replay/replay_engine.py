"""
services/integration/replay/replay_engine.py
=================================
M4.2 §1 Replay Engine — the "time-travel debugger" the M4 roadmap asks
for: a steppable `ReplayTrace` where each step carries the state at
that point and a diff from the previous step, plus `state_at_step` to
jump directly to any point without replaying everything in between.

Two ways to build a trace, because this codebase has one but not the
other available today:

  - `build_trace_from_checkpoints(checkpointer, config)` — the "real"
    replay engine, walking actual LangGraph checkpoint history via
    `checkpoint_browser.py`. This is what M4's roadmap describes, but
    it needs a concrete checkpointer wired to a workflow thread, which
    (per `versioning/checkpoint_migration.py`'s documented limitation)
    doesn't exist anywhere in this codebase yet — no `build_*_graph()`
    call is ever given a live `PostgresSaver`. Verified end-to-end
    against a real `MemorySaver`, same as that module was.

  - `build_trace_from_audit_trail(project_id, events)` — a fallback
    that reconstructs an equivalent trace from `AuditEvent.payload`
    history (the same source `execution_timeline.py` reads), so replay
    is actually usable against this platform's real, current state
    (every project's history lives in `audit_events`, not in any
    checkpoint table) rather than being purely aspirational pending
    future checkpointer wiring. Each audit event is treated as one
    "step" whose state is its payload; this is a coarser grain than a
    true per-superstep checkpoint trace, but the mechanics — diff
    between consecutive steps, jump to any step, replay forward — are
    identical, so nothing here needs to change once a real checkpointer
    lands; only which builder a caller uses does.

Both builders produce the same `ReplayTrace` shape, so
`replay_engine.state_at_step`/`diff_between_steps` work identically
regardless of which source built the trace.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from services.integration.replay import checkpoint_browser as cb
from services.integration.replay.state_diff import StateDiff, diff_states


class ReplayStep(BaseModel):
    index: int
    label: str                      # checkpoint_id, or the audit event_type, depending on source
    source: str                     # "checkpoint" | "audit_trail"
    ts: Optional[str] = None
    state: Dict[str, Any] = Field(default_factory=dict)
    diff_from_previous: Optional[StateDiff] = None


class ReplayTrace(BaseModel):
    subject_id: str                 # thread_id for checkpoints, project_id for audit trail
    source: str                     # "checkpoint" | "audit_trail"
    step_count: int
    steps: List[ReplayStep] = Field(default_factory=list)


def _build_trace(subject_id: str, source: str, labeled_states: List[Dict[str, Any]]) -> ReplayTrace:
    """`labeled_states` items: {"label": str, "ts": Optional[str], "state": dict}, oldest first."""
    steps: List[ReplayStep] = []
    previous_state: Optional[Dict[str, Any]] = None
    for i, item in enumerate(labeled_states):
        state = item["state"]
        diff = diff_states(previous_state, state) if previous_state is not None else None
        steps.append(ReplayStep(
            index=i, label=item["label"], source=source, ts=item.get("ts"),
            state=state, diff_from_previous=diff,
        ))
        previous_state = state
    return ReplayTrace(subject_id=subject_id, source=source, step_count=len(steps), steps=steps)


def build_trace_from_checkpoints(checkpointer: Any, config: Any) -> ReplayTrace:
    thread_id = config.get("configurable", {}).get("thread_id", "unknown")
    summaries = cb.list_checkpoints(checkpointer, config)
    labeled_states = []
    for s in summaries:
        detail = cb.get_checkpoint_detail(checkpointer, config, s.checkpoint_id)
        labeled_states.append({"label": s.checkpoint_id, "ts": s.ts, "state": detail.channel_values if detail else {}})
    return _build_trace(thread_id, "checkpoint", labeled_states)


async def abuild_trace_from_checkpoints(checkpointer: Any, config: Any) -> ReplayTrace:
    thread_id = config.get("configurable", {}).get("thread_id", "unknown")
    summaries = await cb.alist_checkpoints(checkpointer, config)
    labeled_states = []
    for s in summaries:
        detail = await cb.aget_checkpoint_detail(checkpointer, config, s.checkpoint_id)
        labeled_states.append({"label": s.checkpoint_id, "ts": s.ts, "state": detail.channel_values if detail else {}})
    return _build_trace(thread_id, "checkpoint", labeled_states)


def build_trace_from_audit_trail(project_id: str, events: List[Any]) -> ReplayTrace:
    """`events` must be oldest-first (same contract as
    execution_timeline.build_execution_timeline). Each event's `payload`
    is treated as that step's full state snapshot — a project's audit
    trail doesn't record cumulative state, only what changed at that
    moment, so unlike a true checkpoint trace this doesn't automatically
    carry forward keys a later event's payload didn't repeat. Documented
    here rather than silently merging payloads together, which would
    quietly invent state the platform never actually recorded as such."""
    labeled_states = []
    for ev in events:
        ts = ev.recorded_at
        ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        labeled_states.append({"label": ev.event_type, "ts": ts_str, "state": dict(ev.payload or {})})
    return _build_trace(project_id, "audit_trail", labeled_states)


async def get_audit_trail_trace(db: Any, project_id: str, limit: int = 1000) -> ReplayTrace:
    from infrastructure.database.repositories import AuditRepository  # local import: avoid import cycles

    events = await AuditRepository.list_for_project_ascending(db, project_id, limit=limit)
    return build_trace_from_audit_trail(project_id, events)


def state_at_step(trace: ReplayTrace, index: int) -> Optional[Dict[str, Any]]:
    """Direct jump to any step's state — no need to replay 0..index in
    order, since every step's full state (not just its diff) is
    retained on its `ReplayStep`."""
    if index < 0 or index >= len(trace.steps):
        return None
    return trace.steps[index].state


def diff_between_steps(trace: ReplayTrace, from_index: int, to_index: int) -> Optional[StateDiff]:
    from_state = state_at_step(trace, from_index)
    to_state = state_at_step(trace, to_index)
    if from_state is None or to_state is None:
        return None
    return diff_states(from_state, to_state)
