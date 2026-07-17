"""
services/integration/diagnostics/graph_exporter.py
=================================
M3.10 §2 Mermaid Export — turns each registered LangGraph's compiled
edge list into a `graph TD` Mermaid diagram, and writes one Markdown
file per workflow under docs/workflows/, plus a workflow_index.md
summary table.

Kept separate from workflow_docs.py: this module only knows how to
turn a graph into Mermaid syntax; workflow_docs.py knows how to turn a
WorkflowReport + Mermaid diagram into a full narrative doc page. Both
are consumed by scripts/generate_workflow_docs.py (and by the
diagnostics API's `/platform/workflows/mermaid/{workflow}` endpoint).
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

from services.integration import lifecycle
from services.integration.validators.workflow_validator import WorkflowReport, analyze_workflow

START = lifecycle.START
END = lifecycle.END

_MERMAID_START = "START"
_MERMAID_END = "END"


def _mermaid_id(node: str) -> str:
    """Mermaid node IDs can't contain spaces or certain punctuation;
    every node name in this codebase is already a valid identifier
    (snake_case), so this is a defensive no-op passthrough plus the
    START/END rename for readability."""
    if node == START:
        return _MERMAID_START
    if node == END:
        return _MERMAID_END
    return node


def build_graph_edges(name: str) -> Tuple[List[str], List[Tuple[str, str, bool]], str]:
    """Re-resolves and builds the named workflow directly from the
    registry (fresh, not cached) and returns (nodes, edges_with_conditional_flag, error).
    `error` is empty string on success."""
    for entry in lifecycle._graph_registry():  # noqa: SLF001
        if entry["name"] != name:
            continue
        if entry.get("builder") is None:
            return [], [], entry.get("import_error", "workflow not found")
        try:
            compiled = entry["builder"](**(entry.get("kwargs") or {}))
            gg = compiled.get_graph()
            nodes = list(gg.nodes.keys())
            edges = [(e.source, e.target, bool(getattr(e, "conditional", False))) for e in gg.edges]
            return nodes, edges, ""
        except Exception as e:  # noqa: BLE001
            return [], [], str(e)
    return [], [], f"unknown workflow {name!r}"


def _edge_labels(report: WorkflowReport) -> Dict[Tuple[str, str], str]:
    """Inverts WorkflowReport.conditional_routes (source -> {outcome:
    target}) into {(source, target): outcome} so each Mermaid edge can
    carry its routing label (e.g. `-->|approved|`)."""
    labels: Dict[Tuple[str, str], str] = {}
    for route in report.conditional_routes:
        for outcome, target in route.outcomes.items():
            labels[(route.source, target)] = outcome
    return labels


def generate_mermaid(name: str) -> str:
    """Returns a fenced ```mermaid code block (without the fence, callers
    add it) for one workflow. Never raises — an unbuildable workflow
    still produces a small diagram noting the error."""
    nodes, edges, error = build_graph_edges(name)
    if error:
        return f'graph TD\n    ERROR["workflow {name!r} could not be built: {error}"]'

    report = analyze_workflow(name, *_resolve_builder(name))
    labels = _edge_labels(report)

    lines = ["graph TD"]
    seen_nodes = set()
    for node in nodes:
        mid = _mermaid_id(node)
        if mid in seen_nodes:
            continue
        seen_nodes.add(mid)
        if node == START:
            lines.append(f"    {mid}([START])")
        elif node == END:
            lines.append(f"    {mid}([END])")
        else:
            shape = "{{" + node + "}}" if any(r.source == node for r in report.conditional_routes) else f"[{node}]"
            lines.append(f"    {mid}{shape}")

    for source, target, conditional in edges:
        s_id, t_id = _mermaid_id(source), _mermaid_id(target)
        label = labels.get((source, target))
        if label:
            lines.append(f"    {s_id} -->|{label}| {t_id}")
        elif conditional:
            lines.append(f"    {s_id} -.-> {t_id}")
        else:
            lines.append(f"    {s_id} --> {t_id}")

    return "\n".join(lines)


def _resolve_builder(name: str):
    """Returns (builder, kwargs, module, import_error) for analyze_workflow,
    reusing the same registry entry build_graph_edges just used."""
    for entry in lifecycle._graph_registry():  # noqa: SLF001
        if entry["name"] == name:
            return entry.get("builder"), entry.get("kwargs"), entry.get("module"), entry.get("import_error")
    return None, {}, None, f"unknown workflow {name!r}"


def generate_all_mermaid() -> Dict[str, str]:
    return {entry["name"]: generate_mermaid(entry["name"]) for entry in lifecycle._graph_registry()}  # noqa: SLF001


def write_mermaid_docs(output_dir: str = "docs/workflows") -> List[str]:
    """Writes one `{workflow}.md` per registered workflow (just the
    Mermaid diagram — workflow_docs.write_all_docs() writes the fuller
    narrative pages that embed these same diagrams) plus
    workflow_index.md. Returns the list of file paths written."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: List[str] = []

    diagrams = generate_all_mermaid()
    index_rows: List[str] = [
        "| Workflow | Nodes | Edges | Interrupts | Parallel Branches | Graph Depth |",
        "|---|---|---|---|---|---|",
    ]
    for name in sorted(diagrams):
        report = analyze_workflow(name, *_resolve_builder(name))
        path = out / f"{name}.md"
        path.write_text(
            f"# Workflow: {name}\n\n"
            f"Healthy: {'✓' if report.healthy else '✗'}\n\n"
            f"```mermaid\n{diagrams[name]}\n```\n"
        )
        written.append(str(path))
        index_rows.append(
            f"| {name} | {report.node_count} | {report.edge_count} | "
            f"{len(report.interrupt_nodes)} | {len(report.parallel_branches)} | {report.graph_depth} |"
        )

    index_path = out / "workflow_index.md"
    index_path.write_text("# Workflow Index\n\n" + "\n".join(index_rows) + "\n")
    written.append(str(index_path))
    return written
