"""
services/integration/replay/state_diff.py
=================================
M4.2 §3 State Diff — a generic, framework-free diff between two plain
state dicts (a LangGraph checkpoint's `channel_values`, an
`AuditEvent.payload`, an `Artifact.content`, or any other JSON-shaped
snapshot the platform produces). Mirrors `versioning/graph_diff.py`'s
convention of pure functions over plain data so this is independently
unit-testable and reusable by `execution_timeline.py`, `replay_engine.py`,
and `artifact_diff.py` without any of them depending on each other's
internals.

Deliberately shallow-plus-recursive rather than a generic deep-diff
library: dict values are diffed recursively (so a nested `content.sections`
change is reported at the path it actually occurred, e.g.
`sections.0.title`, not just "content changed"); list values are
compared by position (index-based), which is the right semantics for
ordered pipeline output (task lists, review findings, timeline
entries) even though it isn't a minimal-edit-distance list diff — that
tradeoff is documented here rather than silently assumed.
"""
from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, Field

_SENTINEL = object()


class FieldChange(BaseModel):
    path: str
    before: Any = None
    after: Any = None


class StateDiff(BaseModel):
    keys_added: List[str] = Field(default_factory=list)
    keys_removed: List[str] = Field(default_factory=list)
    changed: List[FieldChange] = Field(default_factory=list)
    identical: bool = True

    @property
    def changed_paths(self) -> List[str]:
        return [c.path for c in self.changed]


def _walk(before: Any, after: Any, path: str, changed: List[FieldChange]) -> None:
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            sub_path = f"{path}.{key}" if path else key
            b = before.get(key, _SENTINEL)
            a = after.get(key, _SENTINEL)
            if b is _SENTINEL or a is _SENTINEL:
                changed.append(FieldChange(
                    path=sub_path, before=None if b is _SENTINEL else b, after=None if a is _SENTINEL else a))
                continue
            _walk(b, a, sub_path, changed)
        return

    if isinstance(before, list) and isinstance(after, list):
        max_len = max(len(before), len(after))
        for i in range(max_len):
            sub_path = f"{path}.{i}" if path else str(i)
            b = before[i] if i < len(before) else _SENTINEL
            a = after[i] if i < len(after) else _SENTINEL
            if b is _SENTINEL or a is _SENTINEL:
                changed.append(FieldChange(
                    path=sub_path, before=None if b is _SENTINEL else b, after=None if a is _SENTINEL else a))
                continue
            _walk(b, a, sub_path, changed)
        return

    if not _equal_leaf(before, after):
        changed.append(FieldChange(path=path or "$", before=before, after=after))


def _equal_leaf(a: Any, b: Any) -> bool:
    try:
        return a == b
    except Exception:  # noqa: BLE001 — never let an exotic __eq__ crash the diff
        return a is b


def diff_states(before: Dict[str, Any], after: Dict[str, Any]) -> StateDiff:
    """Never raises. `before`/`after` default to empty dicts semantics
    via normal dict access; non-dict top-level input is treated as a
    single `$`-path change rather than erroring, so a malformed payload
    degrades to "everything changed" instead of crashing the caller."""
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}

    keys_added = sorted(set(after) - set(before))
    keys_removed = sorted(set(before) - set(after))

    changed: List[FieldChange] = []
    for key in sorted(set(before) & set(after)):
        _walk(before[key], after[key], key, changed)

    identical = not (keys_added or keys_removed or changed)
    return StateDiff(keys_added=keys_added, keys_removed=keys_removed, changed=changed, identical=identical)
