"""
services/integration/workflow_designer/designer_export.py
=================================
M4.8 §16 Export — JSON/YAML (full round-trip layout, delegated to
`workflow_serializer.py`), Mermaid (delegated to `graph_builder.to_mermaid`),
and Markdown documentation. "Reuse documentation generators" — this
sandbox slice does not include M3's real workflow-documentation generator
module (see docs/M4.8_Workflow_Designer_Handover.md §3); `render_markdown`
first tries `services.workflow.doc_generator.render_workflow_doc` and only
falls back to the small self-contained renderer below when that import
doesn't resolve, same convention `graph_builder.to_mermaid` uses for the
real Mermaid exporter.
"""
from __future__ import annotations

import importlib

from services.integration.workflow_designer.designer_models import WorkflowLayout
from services.integration.workflow_designer.graph_builder import build_graph, to_mermaid
from services.integration.workflow_designer.workflow_serializer import SerializerError, export_json, export_yaml


def _fallback_markdown(layout: WorkflowLayout) -> str:
    result = build_graph(layout)
    lines = [f"# Workflow: {layout.workflow_name}", "", f"Version: `{layout.version}`",
              f"Entry point: `{result.entry_point or 'none'}`", "", "## Nodes", "",
              "| id | type | label |", "|---|---|---|"]
    for n in result.nodes:
        lines.append(f"| {n['id']} | {n['node_type']} | {n.get('label', '')} |")
    lines += ["", "## Edges", "", "| source | target | condition |", "|---|---|---|"]
    for e in result.edges:
        lines.append(f"| {e['source']} | {e['target']} | {e.get('condition', '')} |")
    if result.warnings:
        lines += ["", "## Warnings", ""]
        lines += [f"- {w}" for w in result.warnings]
    return "\n".join(lines)


def render_markdown(layout: WorkflowLayout) -> str:
    try:
        module = importlib.import_module("services.workflow.doc_generator")
        fn = getattr(module, "render_workflow_doc")
    except Exception:  # noqa: BLE001 — not available in this sandbox slice; use fallback
        return _fallback_markdown(layout)
    try:
        result = build_graph(layout)
        return fn({"nodes": result.nodes, "edges": result.edges, "entry_point": result.entry_point})
    except Exception:  # noqa: BLE001 — real generator present but incompatible signature; fall back
        return _fallback_markdown(layout)


_RENDERERS = {
    "json": lambda layout: export_json(layout),
    "yaml": lambda layout: export_yaml(layout),
    "mermaid": lambda layout: to_mermaid(layout),
    "markdown": lambda layout: render_markdown(layout),
}


def export_workflow(layout: WorkflowLayout, fmt: str) -> str:
    """§16 entry point — dispatches by format name, one function for
    `designer_cli.py`/`POST /designer/export` to call."""
    try:
        return _RENDERERS[fmt](layout)
    except KeyError:
        raise SerializerError(f"unknown export format {fmt!r}; choose one of {sorted(_RENDERERS)}") from None
