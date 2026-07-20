"""
services/integration/workflow_designer/designer_models.py
=================================
M4.8 — pure Pydantic shapes, no FastAPI/SQLAlchemy/NetworkX import, same
layering convention M4.7's `plugin_models.py` established: plain models so
`canvas_state.py`/`graph_builder.py`/`workflow_serializer.py`/
`workflow_deserializer.py`/`validation_bridge.py` stay independently
unit-testable, with no persistence or HTTP machinery anywhere near the data
shapes themselves.

**Canvas vs. Graph — two different shapes on purpose.** `WorkflowLayout` is
the *designer's* document: nodes carry `x`/`y`/`color`/`comment` alongside
their runtime `node_type`/`config`, and the document as a whole carries a
`CanvasViewport` (zoom/pan) that has no meaning to the LangGraph runtime at
all. `graph_builder.py` is the one module that narrows a `WorkflowLayout`
down to the pure `{nodes, edges, entry_point}` runtime shape the brief's §4
"must generate identical graph structures as handwritten workflows" targets
— layout/presentation fields never leak into that narrower shape. Keeping
these as two separate Pydantic models (rather than one model with "runtime"
and "designer" field groups) is what makes "Designer remains read-only
during replay mode" (§9) and "never modify existing workflow runtime logic"
enforceable structurally: nothing downstream of `graph_builder.build_graph`
can see designer-only fields, because they were never on that shape.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class NodeType(str, Enum):
    """§2 Node Library's catalog, verbatim, plus PLUGIN for a
    plugin-registered node (see `node_library.py`'s
    `plugin_node_templates`) and CUSTOM for a user-defined category
    (§2 "Support custom node categories")."""
    AGENT = "agent"
    DECISION = "decision"
    CONDITIONAL = "conditional"
    PARALLEL = "parallel"   # LangGraph Send
    JOIN = "join"
    TOOL = "tool"
    EVENT = "event"
    HUMAN_APPROVAL = "human_approval"
    RETRY = "retry"
    DELAY = "delay"
    PLUGIN = "plugin"
    CUSTOM = "custom"


class ExportFormat(str, Enum):
    JSON = "json"
    YAML = "yaml"
    MERMAID = "mermaid"
    MARKDOWN = "markdown"


class DesignerNode(BaseModel):
    """One canvas node. `node_type`/`config` are the runtime-meaningful
    fields `graph_builder.py` reads; `x`/`y`/`color`/`comment`/`group_id`
    are designer-only (§1 "Nodes... Coordinates... Comments... Colors")
    and are never inspected by `graph_builder.py`."""
    id: str
    node_type: NodeType
    label: str = ""
    x: float = 0.0
    y: float = 0.0
    color: Optional[str] = None
    comment: Optional[str] = None
    group_id: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)
    plugin_id: Optional[str] = None  # set when node_type == PLUGIN


class DesignerEdge(BaseModel):
    """One canvas edge. `condition` carries a conditional/decision edge's
    branch predicate (a plain string expression, evaluated by the existing
    runtime — this package never evaluates it); `label` is designer-only
    presentation text."""
    id: str
    source: str
    target: str
    label: str = ""
    condition: Optional[str] = None


class DesignerGroup(BaseModel):
    """§1 "Groups" — a named, colored rectangle a designer can gather
    nodes into for visual organization. Purely presentational; a group has
    no runtime meaning to `graph_builder.py`."""
    id: str
    label: str = ""
    color: Optional[str] = None
    node_ids: List[str] = Field(default_factory=list)


class CanvasViewport(BaseModel):
    """§3 Canvas State's zoom/pan, persisted alongside the layout so
    reopening a workflow restores the last view."""
    zoom: float = 1.0
    pan_x: float = 0.0
    pan_y: float = 0.0


class WorkflowLayout(BaseModel):
    """The designer's full document for one workflow — §1's "editable
    canvas objects," serializable per §5. `workflow_name` is the id
    `graph_builder.py`/the version registry bridge key results by;
    `entry_node_id` is the canvas's chosen start node (LangGraph's entry
    point) and may be `None` for a layout still in progress (see
    `graph_builder.build_graph`'s handling of that case)."""
    workflow_name: str
    version: str = "0.0.1"
    nodes: List[DesignerNode] = Field(default_factory=list)
    edges: List[DesignerEdge] = Field(default_factory=list)
    groups: List[DesignerGroup] = Field(default_factory=list)
    entry_node_id: Optional[str] = None
    viewport: CanvasViewport = Field(default_factory=CanvasViewport)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    updated_at: str = ""


class CanvasSelection(BaseModel):
    """§3 Canvas State's selection set — node/edge/group ids currently
    selected, frontend-agnostic (no DOM/SVG element references)."""
    node_ids: List[str] = Field(default_factory=list)
    edge_ids: List[str] = Field(default_factory=list)
    group_ids: List[str] = Field(default_factory=list)


class ClipboardPayload(BaseModel):
    """§3 "Clipboard" — a self-contained cut/copy of nodes+edges (edges
    whose endpoints are both in the copied node set), ready to paste at an
    offset via `canvas_state.paste`."""
    nodes: List[DesignerNode] = Field(default_factory=list)
    edges: List[DesignerEdge] = Field(default_factory=list)


class CanvasState(BaseModel):
    """§3 Canvas State in full: the current `WorkflowLayout` plus
    undo/redo history, selection, clipboard, and autosave bookkeeping.
    `history`/`future` hold prior/next `WorkflowLayout` snapshots (a
    simple, brief-appropriate undo model — see `canvas_state.py`'s module
    docstring for why snapshot-based undo was chosen over an operation
    log)."""
    layout: WorkflowLayout
    selection: CanvasSelection = Field(default_factory=CanvasSelection)
    clipboard: Optional[ClipboardPayload] = None
    history: List[WorkflowLayout] = Field(default_factory=list)
    future: List[WorkflowLayout] = Field(default_factory=list)
    dirty: bool = False
    last_autosaved_at: Optional[str] = None


class NodeTemplate(BaseModel):
    """§2 Node Library entry — one node type a user can drag onto the
    canvas. `default_config` seeds a new `DesignerNode.config`;
    `property_schema` is a flat field-name -> type-hint map a property
    panel renders from (deliberately not a full JSON Schema document —
    the brief's UI section asks for a property panel, not a schema
    validator; `validation_bridge.py` is where real validation happens)."""
    node_type: NodeType
    category: str
    label: str
    description: str = ""
    icon: str = "circle"
    default_config: Dict[str, Any] = Field(default_factory=dict)
    property_schema: Dict[str, str] = Field(default_factory=dict)
    plugin_id: Optional[str] = None
    source: str = "builtin"  # "builtin" | "plugin" | "custom"


class GraphBuildResult(BaseModel):
    """§4 Graph Builder's output — the pure `{nodes, edges, entry_point}`
    runtime shape (see module docstring's "Canvas vs. Graph" note),
    plus `warnings` for anything degraded/skipped during the build
    (e.g. an edge referencing a node id that no longer exists)."""
    workflow_name: str
    nodes: List[Dict[str, Any]] = Field(default_factory=list)
    edges: List[Dict[str, Any]] = Field(default_factory=list)
    entry_point: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)


class ValidationBridgeIssue(BaseModel):
    rule_id: str
    severity: str = "error"  # "error" | "warning"
    message: str
    node_id: Optional[str] = None
    edge_id: Optional[str] = None
    source: str = "designer"  # "designer" | "workflow_validator" | "graph_linter" | "version_registry"


class ValidationBridgeResult(BaseModel):
    """§7 Validation's aggregate output. `sources_available` records
    which of the three brief-named integrations (`workflow_validator`,
    `graph_linter`, `version_registry`) actually ran vs. degraded — see
    `validation_bridge.py`'s module docstring."""
    workflow_name: str
    valid: bool = True
    issues: List[ValidationBridgeIssue] = Field(default_factory=list)
    sources_available: Dict[str, bool] = Field(default_factory=dict)

    @property
    def errors(self) -> List[ValidationBridgeIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> List[ValidationBridgeIssue]:
        return [i for i in self.issues if i.severity == "warning"]


class VersionDiffEntry(BaseModel):
    kind: str  # "node_added" | "node_removed" | "node_moved" | "node_changed" | "edge_added" | "edge_removed"
    node_id: Optional[str] = None
    edge_id: Optional[str] = None
    detail: str = ""


class LayoutDiffResult(BaseModel):
    """§8 Version Integration's "View diffs" output — a layout-aware
    diff between two `WorkflowLayout` snapshots (node/edge add/remove
    plus pure-layout moves), distinct from the runtime-graph diff a real
    `version_registry`/`graph_diff` module would compute over
    `GraphBuildResult` shapes (this package never duplicates that; see
    `validation_bridge.py`)."""
    workflow_name: str
    from_version: str
    to_version: str
    entries: List[VersionDiffEntry] = Field(default_factory=list)

    @property
    def is_breaking(self) -> bool:
        return any(e.kind in ("node_removed", "edge_removed") for e in self.entries)


class ReplayNodeState(BaseModel):
    node_id: str
    status: str = "pending"  # "pending" | "running" | "succeeded" | "failed" | "skipped"
    duration_ms: Optional[float] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class ReplayOverlay(BaseModel):
    """§9 Replay Overlay — the current execution's path/state mapped onto
    canvas node ids, sourced from M4.2's replay engine (see
    `validation_bridge.fetch_replay_overlay`'s graceful-degradation
    fallback when that engine isn't wired in this sandbox slice)."""
    workflow_name: str
    execution_id: str
    current_node_id: Optional[str] = None
    failed_node_id: Optional[str] = None
    node_states: List[ReplayNodeState] = Field(default_factory=list)
    available: bool = True


class DesignerSession(BaseModel):
    """§12 Repository's `DesignerSession` table shape — one open editing
    session for one user against one workflow, tracking autosave/dirty
    state independent of `CanvasSnapshot` history (a session is
    ephemeral bookkeeping; a snapshot is a durable point-in-time
    layout)."""
    id: str
    workflow_name: str
    user_id: Optional[str] = None
    opened_at: str = ""
    last_activity_at: str = ""
    dirty: bool = False
    replay_mode: bool = False


class CanvasSnapshot(BaseModel):
    """§12 Repository's `CanvasSnapshot` table shape — one durable,
    named point-in-time capture of a `WorkflowLayout` (an explicit save,
    an autosave tick, or a pre-import backup)."""
    id: str
    workflow_name: str
    layout: WorkflowLayout
    reason: str = "save"  # "save" | "autosave" | "pre_import" | "version_restore"
    created_at: str = ""


class DesignerPluginNodeAction(BaseModel):
    """§10 Plugin Integration — one plugin-registered extension point
    (custom node / property editor / validation rule / toolbar action /
    context menu action), discovered from an enabled plugin's manifest
    metadata (see `node_library.plugin_node_templates`'s module
    docstring on where that metadata is expected to live)."""
    plugin_id: str
    kind: str  # "node" | "property_editor" | "validation_rule" | "toolbar_action" | "context_menu_action"
    key: str
    label: str = ""


class DesignerLibrary(BaseModel):
    """The full §2 Node Library payload the API/SPA fetch once at
    startup: builtin templates + plugin templates + custom categories,
    already merged and de-duplicated by `node_library.build_library`."""
    templates: List[NodeTemplate] = Field(default_factory=list)
    categories: List[str] = Field(default_factory=list)
    plugin_actions: List[DesignerPluginNodeAction] = Field(default_factory=list)
    generated_at: str = ""
