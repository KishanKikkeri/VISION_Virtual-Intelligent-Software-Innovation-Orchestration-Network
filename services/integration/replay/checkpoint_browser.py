"""
services/integration/replay/checkpoint_browser.py
=================================
M4.2 §5 Checkpoint Browser — a normalized, read-only view over a
LangGraph checkpointer's history for one thread. Duck-types against
`list`/`get_tuple` (`alist`/`aget_tuple` async), the same protocol
`services/integration/versioning/checkpoint_migration.py` targets, so
this works against any concrete checkpointer (`MemorySaver` today;
`PostgresSaver`/`AsyncPostgresSaver` once wired, per that module's
documented limitation that no live Postgres checkpointer exists yet in
this codebase) without depending on which one is in use.

LangGraph's own `checkpointer.list()` yields `CheckpointTuple` objects
most-recent-first with a `parent_config` back-link per tuple; this
module's contribution is reducing that into a flat, oldest-first
`CheckpointSummary` list the rest of M4.2 (`replay_engine.py`) can walk
forward through, plus a `get_checkpoint_detail` lookup that returns the
full `channel_values` for one specific checkpoint (the actual "browse"
part — `list_checkpoints` alone only gives metadata, deliberately: full
state blobs for every checkpoint in a long-running thread could be
large, so callers fetch detail only for the checkpoint(s) they're
actually inspecting).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class CheckpointSummary(BaseModel):
    checkpoint_id: str
    parent_checkpoint_id: Optional[str] = None
    source: Optional[str] = None
    step: Optional[int] = None
    ts: Optional[str] = None


class CheckpointDetail(CheckpointSummary):
    channel_values: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


def _summarize(checkpoint_tuple: Any) -> CheckpointSummary:
    checkpoint = checkpoint_tuple.checkpoint
    metadata = checkpoint_tuple.metadata or {}
    parent_config = checkpoint_tuple.parent_config
    parent_id = None
    if parent_config:
        parent_id = parent_config.get("configurable", {}).get("checkpoint_id")
    return CheckpointSummary(
        checkpoint_id=checkpoint.get("id"),
        parent_checkpoint_id=parent_id,
        source=metadata.get("source"),
        step=metadata.get("step"),
        ts=checkpoint.get("ts"),
    )


def _detail(checkpoint_tuple: Any) -> CheckpointDetail:
    summary = _summarize(checkpoint_tuple)
    return CheckpointDetail(
        **summary.model_dump(),
        channel_values=dict(checkpoint_tuple.checkpoint.get("channel_values") or {}),
        metadata=dict(checkpoint_tuple.metadata or {}),
    )


def list_checkpoints(checkpointer: Any, config: Any, limit: Optional[int] = None) -> List[CheckpointSummary]:
    """Sync path. Returns oldest-first (reversing LangGraph's native
    most-recent-first iteration order) since a browser/timeline UI
    reads more naturally top-to-bottom-in-time, matching
    execution_timeline.py's ordering convention."""
    tuples = list(checkpointer.list(config, limit=limit))
    return [_summarize(t) for t in reversed(tuples)]


async def alist_checkpoints(checkpointer: Any, config: Any, limit: Optional[int] = None) -> List[CheckpointSummary]:
    tuples = [t async for t in checkpointer.alist(config, limit=limit)]
    return [_summarize(t) for t in reversed(tuples)]


def get_checkpoint_detail(checkpointer: Any, config: Any, checkpoint_id: str) -> Optional[CheckpointDetail]:
    """Fetches full state for one checkpoint by id. LangGraph's
    `get_tuple` resolves to the *latest* checkpoint unless `config`
    already carries `configurable.checkpoint_id` — so this sets it on
    a copy of `config` rather than mutating the caller's dict."""
    scoped_config = _with_checkpoint_id(config, checkpoint_id)
    checkpoint_tuple = checkpointer.get_tuple(scoped_config)
    if checkpoint_tuple is None:
        return None
    return _detail(checkpoint_tuple)


async def aget_checkpoint_detail(checkpointer: Any, config: Any, checkpoint_id: str) -> Optional[CheckpointDetail]:
    scoped_config = _with_checkpoint_id(config, checkpoint_id)
    checkpoint_tuple = await checkpointer.aget_tuple(scoped_config)
    if checkpoint_tuple is None:
        return None
    return _detail(checkpoint_tuple)


def _with_checkpoint_id(config: Any, checkpoint_id: str) -> Dict[str, Any]:
    scoped = {**config, "configurable": {**config.get("configurable", {}), "checkpoint_id": checkpoint_id}}
    return scoped
