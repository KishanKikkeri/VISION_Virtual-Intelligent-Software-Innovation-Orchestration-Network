"""
services/integration/workflow_designer/canvas_state.py
=================================
M4.8 §3 Canvas State — zoom/pan/selection/undo/redo/clipboard/autosave,
entirely frontend-agnostic (brief's own wording): every function here
takes and returns plain `designer_models.CanvasState`/`WorkflowLayout`
values, no DOM/SVG/browser-event objects anywhere.

**Undo model: whole-layout snapshots, not an operation log.** A
`WorkflowLayout` in this milestone's realistic size (tens of nodes, not
thousands) is cheap enough to deep-copy on every history push that a
snapshot stack is simpler and more robust than replaying an operation
log (no risk of an undo/redo operation drifting from forward-apply
semantics as new operation types are added later). `push_history` is the
one function every mutating canvas operation (`add_node`, `move_node`,
`delete_selection`, `paste`, ...) calls before applying its own change.

**Autosave** is state, not a timer — `mark_autosaved` is called by
whatever caller owns real wall-clock scheduling (the API layer/SPA); this
module only tracks *whether* the current layout has been autosaved since
its last change (`dirty`) and *when* (`last_autosaved_at`).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from services.integration.workflow_designer.designer_models import (
    CanvasSelection, CanvasState, ClipboardPayload, DesignerEdge, DesignerNode, WorkflowLayout,
)

MAX_HISTORY = 100  # bounds unbounded growth across a very long editing session


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_canvas_state(layout: WorkflowLayout) -> CanvasState:
    return CanvasState(layout=layout)


def push_history(state: CanvasState) -> CanvasState:
    """Snapshots the current layout onto `history` and clears `future`
    (a fresh edit after an undo invalidates the old redo branch — the
    same convention every standard undo/redo stack uses). Marks `dirty`
    and bumps `updated_at`. Callers apply their actual mutation to
    `state.layout` *after* calling this."""
    history = (state.history + [state.layout.model_copy(deep=True)])[-MAX_HISTORY:]
    layout = state.layout.model_copy(update={"updated_at": _now_iso()})
    return state.model_copy(update={"history": history, "future": [], "layout": layout, "dirty": True})


def undo(state: CanvasState) -> CanvasState:
    if not state.history:
        return state
    previous = state.history[-1]
    new_history = state.history[:-1]
    new_future = [state.layout.model_copy(deep=True)] + state.future
    return state.model_copy(update={"layout": previous, "history": new_history, "future": new_future, "dirty": True})


def redo(state: CanvasState) -> CanvasState:
    if not state.future:
        return state
    next_layout = state.future[0]
    new_future = state.future[1:]
    new_history = state.history + [state.layout.model_copy(deep=True)]
    return state.model_copy(update={"layout": next_layout, "history": new_history, "future": new_future,
                                     "dirty": True})


def add_node(state: CanvasState, node: DesignerNode) -> CanvasState:
    state = push_history(state)
    nodes = [n for n in state.layout.nodes if n.id != node.id] + [node]
    state.layout.nodes = nodes
    return state


def move_node(state: CanvasState, node_id: str, x: float, y: float) -> CanvasState:
    state = push_history(state)
    for n in state.layout.nodes:
        if n.id == node_id:
            n.x, n.y = x, y
    return state


def update_node_config(state: CanvasState, node_id: str, config: dict) -> CanvasState:
    state = push_history(state)
    for n in state.layout.nodes:
        if n.id == node_id:
            n.config = {**n.config, **config}
    return state


def add_edge(state: CanvasState, edge: DesignerEdge) -> CanvasState:
    state = push_history(state)
    edges = [e for e in state.layout.edges if e.id != edge.id] + [edge]
    state.layout.edges = edges
    return state


def set_selection(state: CanvasState, node_ids: Optional[List[str]] = None,
                   edge_ids: Optional[List[str]] = None, group_ids: Optional[List[str]] = None) -> CanvasState:
    """Selection changes do not push undo history — selecting things is
    not a document mutation."""
    return state.model_copy(update={"selection": CanvasSelection(
        node_ids=node_ids or [], edge_ids=edge_ids or [], group_ids=group_ids or [],
    )})


def delete_selection(state: CanvasState) -> CanvasState:
    """Deletes every selected node/edge/group, plus any edge whose
    endpoint was a deleted node (an edge cannot dangle) — same
    referential-integrity convention `graph_builder.py` assumes when it
    later reads this layout."""
    state = push_history(state)
    node_ids = set(state.selection.node_ids)
    edge_ids = set(state.selection.edge_ids)
    group_ids = set(state.selection.group_ids)

    state.layout.nodes = [n for n in state.layout.nodes if n.id not in node_ids]
    state.layout.edges = [
        e for e in state.layout.edges
        if e.id not in edge_ids and e.source not in node_ids and e.target not in node_ids
    ]
    state.layout.groups = [g for g in state.layout.groups if g.id not in group_ids]
    for g in state.layout.groups:
        g.node_ids = [nid for nid in g.node_ids if nid not in node_ids]
    if state.layout.entry_node_id in node_ids:
        state.layout.entry_node_id = None
    state.selection = CanvasSelection()
    return state


def copy_selection(state: CanvasState) -> CanvasState:
    """§3 "Clipboard" — copies selected nodes plus any edge whose *both*
    endpoints are in the copied node set (a partial edge would be
    meaningless once pasted)."""
    node_ids = set(state.selection.node_ids)
    nodes = [n.model_copy(deep=True) for n in state.layout.nodes if n.id in node_ids]
    edges = [e.model_copy(deep=True) for e in state.layout.edges if e.source in node_ids and e.target in node_ids]
    return state.model_copy(update={"clipboard": ClipboardPayload(nodes=nodes, edges=edges)})


def cut_selection(state: CanvasState) -> CanvasState:
    state = copy_selection(state)
    return delete_selection(state)


def paste(state: CanvasState, offset_x: float = 40.0, offset_y: float = 40.0,
          id_suffix: str = "_copy") -> CanvasState:
    """Pastes `state.clipboard` at an offset with remapped ids (§3
    "Clipboard"), so pasting never silently collides with existing node
    ids. A `None`/empty clipboard is a no-op, not an error — pasting
    with nothing copied yet is a normal UI state, not a mistake."""
    if state.clipboard is None or not state.clipboard.nodes:
        return state
    state = push_history(state)

    id_map = {}
    new_nodes = []
    for n in state.clipboard.nodes:
        new_id = f"{n.id}{id_suffix}"
        suffix = 1
        while any(existing.id == new_id for existing in state.layout.nodes) or new_id in id_map.values():
            suffix += 1
            new_id = f"{n.id}{id_suffix}{suffix}"
        id_map[n.id] = new_id
        clone = n.model_copy(update={"id": new_id, "x": n.x + offset_x, "y": n.y + offset_y})
        new_nodes.append(clone)

    new_edges = []
    for e in state.clipboard.edges:
        if e.source in id_map and e.target in id_map:
            new_edges.append(e.model_copy(update={
                "id": f"{e.id}{id_suffix}", "source": id_map[e.source], "target": id_map[e.target],
            }))

    state.layout.nodes = state.layout.nodes + new_nodes
    state.layout.edges = state.layout.edges + new_edges
    state.selection = CanvasSelection(node_ids=list(id_map.values()))
    return state


def set_viewport(state: CanvasState, zoom: float, pan_x: float, pan_y: float) -> CanvasState:
    """Viewport changes (§3 "Zoom... Pan") do not push undo history —
    the same "presentation, not document mutation" reasoning as
    `set_selection`; a user should never need to "undo" a zoom."""
    layout = state.layout.model_copy()
    layout.viewport = layout.viewport.model_copy(update={"zoom": zoom, "pan_x": pan_x, "pan_y": pan_y})
    return state.model_copy(update={"layout": layout})


def mark_autosaved(state: CanvasState) -> CanvasState:
    """§3 "Autosave state" — clears `dirty` and records the autosave
    timestamp. Does not touch `history`/`future` (an autosave is a
    checkpoint of the current state, not an undoable edit)."""
    return state.model_copy(update={"dirty": False, "last_autosaved_at": _now_iso()})


def needs_autosave(state: CanvasState, interval_seconds: float = 30.0,
                    now: Optional[datetime] = None) -> bool:
    """True when the layout is dirty and either never autosaved or the
    configured interval has elapsed — the pure predicate an API/SPA
    autosave loop consults; this module never starts a timer itself."""
    if not state.dirty:
        return False
    if state.last_autosaved_at is None:
        return True
    now = now or datetime.now(timezone.utc)
    last = datetime.fromisoformat(state.last_autosaved_at)
    return (now - last).total_seconds() >= interval_seconds
