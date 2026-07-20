"""
services/integration/workflow_designer/graph_builder.py
=================================
M4.8 §4 Graph Builder — narrows a designer `WorkflowLayout` down to the
pure `{nodes, edges, entry_point}` runtime shape (`GraphBuildResult`) a
LangGraph `StateGraph` is built from, and (§6) exports the same layout to
Mermaid flowchart text.

**"Must generate identical graph structures as handwritten workflows"
(§4).** This module never emits designer-only fields (`x`/`y`/`color`/
`comment`/`group_id`/viewport) into `GraphBuildResult` — see
`designer_models.py`'s "Canvas vs. Graph" note. A `DesignerNode`'s
`node_type`+`config` map onto exactly the fields a handwritten LangGraph
node registration already uses (`add_node(node_id, ...)`), and a
`DesignerEdge` maps onto `add_edge`/`add_conditional_edges` the same way
— so two workflows that are graph-structurally identical (same nodes,
same edges, same entry point) always produce byte-identical
`GraphBuildResult.model_dump()` output regardless of layout (coordinates,
colors, comments never affect the runtime shape).

**Mermaid export reuse (§6).** The brief says "Reuse existing Mermaid
exporter" — this sandbox slice (see docs/M4.8_Workflow_Designer_Handover.md
§3) does not include M3's real `mermaid_exporter` module to import. This
module first tries `services.integration.mermaid_exporter.export_mermaid`
(or `services.workflow.mermaid_exporter.export_graph`, the two most likely
real-platform locations); only if neither import resolves does
`to_mermaid` fall back to the small self-contained renderer below.
"""
from __future__ import annotations

from typing import Any, Dict, List

from services.integration.workflow_designer.designer_models import GraphBuildResult, NodeType, WorkflowLayout

_CONDITIONAL_TYPES = {NodeType.DECISION, NodeType.CONDITIONAL}


def build_graph(layout: WorkflowLayout) -> GraphBuildResult:
    """§4 entry point. Dangling edges (an endpoint id not present in
    `layout.nodes`) are dropped with a warning rather than raising —
    a designer canvas mid-edit legitimately passes through states a
    strict runtime graph never would; `validation_bridge.py` (not this
    module) is where "is this layout deployable" is decided. An
    `entry_node_id` that doesn't reference an existing node is likewise
    dropped to `None` with a warning."""
    warnings: List[str] = []
    node_ids = {n.id for n in layout.nodes}

    nodes: List[Dict[str, Any]] = [
        {"id": n.id, "node_type": n.node_type.value, "config": dict(n.config), "label": n.label}
        for n in layout.nodes
    ]

    edges: List[Dict[str, Any]] = []
    for e in layout.edges:
        if e.source not in node_ids:
            warnings.append(f"edge {e.id!r} dropped: source node {e.source!r} does not exist")
            continue
        if e.target not in node_ids:
            warnings.append(f"edge {e.id!r} dropped: target node {e.target!r} does not exist")
            continue
        edge_entry: Dict[str, Any] = {"id": e.id, "source": e.source, "target": e.target}
        if e.condition:
            edge_entry["condition"] = e.condition
        edges.append(edge_entry)

    entry_point = layout.entry_node_id
    if entry_point is not None and entry_point not in node_ids:
        warnings.append(f"entry_node_id {entry_point!r} does not reference an existing node; dropped")
        entry_point = None
    elif entry_point is None and layout.nodes:
        warnings.append("no entry_node_id set; graph has no declared start node")

    return GraphBuildResult(workflow_name=layout.workflow_name, nodes=nodes, edges=edges,
                             entry_point=entry_point, warnings=warnings)


def diff_structure(a: GraphBuildResult, b: GraphBuildResult) -> bool:
    """True when two build results are structurally identical (same node
    ids/types/configs, same edges, same entry point) — the equality check
    `tests/foundation/test_m48_workflow_designer.py` uses to assert §4's
    "identical graph structures as handwritten workflows" claim; also
    useful to a caller (e.g. CI) diffing a designer-generated graph
    against a hand-authored one."""
    return (
        sorted(a.nodes, key=lambda n: n["id"]) == sorted(b.nodes, key=lambda n: n["id"])
        and sorted(a.edges, key=lambda e: e["id"]) == sorted(b.edges, key=lambda e: e["id"])
        and a.entry_point == b.entry_point
    )


def _fallback_mermaid(layout: WorkflowLayout) -> str:
    """Self-contained Mermaid flowchart renderer — used only when no
    real platform exporter import resolves (see module docstring)."""
    lines = ["flowchart TD"]
    shape_open = {NodeType.DECISION: "{", NodeType.CONDITIONAL: "{", NodeType.EVENT: "((", NodeType.PARALLEL: "(["}
    shape_close = {NodeType.DECISION: "}", NodeType.CONDITIONAL: "}", NodeType.EVENT: "))", NodeType.PARALLEL: "])"}
    for n in layout.nodes:
        open_c, close_c = shape_open.get(n.node_type, "["), shape_close.get(n.node_type, "]")
        label = (n.label or n.id).replace('"', "'")
        marker = " ((entry))" if n.id == layout.entry_node_id else ""
        lines.append(f'    {n.id}{open_c}"{label}{marker}"{close_c}')
    for e in layout.edges:
        arrow = f'-->|{e.label}|' if e.label else "-->"
        lines.append(f"    {e.source} {arrow} {e.target}")
    return "\n".join(lines)


def to_mermaid(layout: WorkflowLayout) -> str:
    """§6 "Export Mermaid" — tries the real platform exporter first (see
    module docstring), falls back to `_fallback_mermaid` otherwise."""
    for module_path, fn_name in (
        ("services.integration.mermaid_exporter", "export_mermaid"),
        ("services.workflow.mermaid_exporter", "export_graph"),
    ):
        try:
            import importlib
            module = importlib.import_module(module_path)
            fn = getattr(module, fn_name)
        except Exception:  # noqa: BLE001 — not available in this sandbox slice; try next / fall back
            continue
        try:
            result = build_graph(layout)
            return fn({"nodes": result.nodes, "edges": result.edges, "entry_point": result.entry_point})
        except Exception:  # noqa: BLE001 — real exporter present but incompatible signature; fall back
            continue
    return _fallback_mermaid(layout)
