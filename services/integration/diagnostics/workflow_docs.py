"""
services/integration/diagnostics/workflow_docs.py
=================================
M3.10 §6 Documentation Generator — builds one full narrative Markdown
page per registered workflow (purpose, node list, routing table,
conditional branches, interrupt nodes, embedded Mermaid diagram,
statistics), plus overwrites docs/workflows/workflow_index.md with a
richer summary than graph_exporter's own bare index (this is the
"final" index; graph_exporter.write_mermaid_docs() is what
scripts/generate_workflow_docs.py calls first for the diagrams, then
this module's write_all_docs() is called to produce/replace the fuller
pages using those same diagrams).
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from services.integration.diagnostics import graph_exporter
from services.integration.validators.workflow_validator import WorkflowReport, analyze_workflow

# One-line purpose statements, hand-written (not derivable from the
# graph structure itself) — kept here rather than invented per-page so
# there's exactly one place to update if a workflow's purpose changes.
WORKFLOW_PURPOSE: Dict[str, str] = {
    "architecture": "Turns approved product requirements into a reviewed, versioned system design "
                     "(infra, security, scaling, integration) ready for Engineering.",
    "devops": "Provisions infrastructure and deploys reviewed artifacts, gated on a manager approval.",
    "engineering": "Implements a feature: task breakdown, parallel backend/frontend/integration build-out, "
                   "review cycles, and a merge-ready pull request.",
    "security": "Runs dependency, secret, and compliance scans over Engineering's output before DevOps.",
    "qa": "Generates and executes unit/integration/regression/performance test suites, dead-lettering "
          "tasks that exhaust retries, and gates publication on coverage + verdict.",
    "monitoring": "Continuously collects infrastructure/application/log/trace signals and raises alerts.",
    "incident_response": "Reacts to raised alerts: triage, diagnosis, remediation, and post-incident report.",
    "manager_lifecycle": "The project's end-to-end phase state machine — intake through deployment and "
                          "monitoring hand-off, with approval gates at requirements/architecture/deployment.",
    "manager_delegation": "Per-task delegation loop: select department/agent/model, monitor progress, "
                          "retry or escalate, validate completion.",
    "repository": "Source-control operations — branch, commit, PR, approval gate, merge, release; retries "
                  "on transient failure.",
}


def _routing_table(report: WorkflowReport) -> str:
    if not report.conditional_routes:
        return "_No conditional routes — this workflow is a linear/static-fan-out pipeline._"
    lines = ["| Source Node | Routing Function | Outcome | Target |", "|---|---|---|---|"]
    for route in report.conditional_routes:
        if not route.outcomes:
            lines.append(f"| {route.source} | {route.function or '—'} | — | — |")
            continue
        for outcome, target in sorted(route.outcomes.items()):
            lines.append(f"| {route.source} | {route.function or '—'} | {outcome} | {target} |")
    return "\n".join(lines)


def _node_list(report: WorkflowReport) -> str:
    lines = [f"- **Entry:** `{report.entry_node}`", f"- **Finish:** `{report.finish_node}`",
              f"- **All nodes ({report.node_count}):** " + ", ".join(f"`{n}`" for n in sorted(report.reachable_nodes)
                                                                       or report.reachable_nodes)]
    return "\n".join(lines)


def _stats_block(report: WorkflowReport) -> str:
    return (
        f"| Metric | Value |\n|---|---|\n"
        f"| Nodes | {report.node_count} |\n"
        f"| Edges | {report.edge_count} |\n"
        f"| Graph depth | {report.graph_depth} |\n"
        f"| Average branching factor | {report.average_branching_factor} |\n"
        f"| Reachability | {report.reachability_pct}% |\n"
        f"| Dead ends | {report.dead_end_count} |\n"
        f"| Cycles detected | {len(report.cycles)} |\n"
        f"| Interrupt nodes | {', '.join(report.interrupt_nodes) or 'none'} |\n"
        f"| Checkpoint-capable | {'yes' if report.checkpoint_capable else 'no'} |\n"
        f"| Parallel branches | {len(report.parallel_branches)} |\n"
    )


def render_workflow_doc(name: str) -> str:
    report = analyze_workflow(name, *graph_exporter._resolve_builder(name))  # noqa: SLF001
    diagram = graph_exporter.generate_mermaid(name)
    purpose = WORKFLOW_PURPOSE.get(name, "_Purpose not yet documented for this workflow._")

    parallel_section = "_No parallel branches._"
    if report.parallel_branches:
        rows = ["| Fan-out Node | Kind | Targets |", "|---|---|---|"]
        for pb in report.parallel_branches:
            rows.append(f"| {pb.fan_out_node} | {pb.kind} | {', '.join(pb.targets)} |")
        parallel_section = "\n".join(rows)

    warnings_section = "\n".join(f"- {w}" for w in report.warnings) or "_None._"
    errors_section = "\n".join(f"- {e}" for e in report.errors) or "_None._"

    return f"""# Workflow: {name}

**Status:** {'✓ healthy' if report.healthy else '✗ unhealthy'}

## Purpose

{purpose}

## Nodes

{_node_list(report)}

## Routing Table

{_routing_table(report)}

## Parallel Branches

{parallel_section}

## Interrupt Nodes

{', '.join(report.interrupt_nodes) or '_None._'}

## Diagram

```mermaid
{diagram}
```

## Statistics

{_stats_block(report)}

## Warnings

{warnings_section}

## Errors

{errors_section}
"""


def write_all_docs(output_dir: str = "docs/workflows") -> List[str]:
    """Writes the full narrative page for every registered workflow, then
    rewrites workflow_index.md as a richer summary (name, purpose,
    healthy, nodes, edges, interrupts, parallel branches, graph depth) —
    this supersedes graph_exporter.write_mermaid_docs()'s bare index
    when both are run (scripts/generate_workflow_docs.py runs
    graph_exporter first, then this)."""
    from services.integration import lifecycle

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: List[str] = []

    names = sorted(entry["name"] for entry in lifecycle._graph_registry())  # noqa: SLF001
    index_rows = [
        "# Workflow Index",
        "",
        "| Workflow | Purpose | Healthy | Nodes | Edges | Interrupts | Parallel | Depth |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name in names:
        report = analyze_workflow(name, *graph_exporter._resolve_builder(name))  # noqa: SLF001
        doc_path = out / f"{name}.md"
        doc_path.write_text(render_workflow_doc(name))
        written.append(str(doc_path))
        purpose_short = WORKFLOW_PURPOSE.get(name, "—")
        if len(purpose_short) > 60:
            purpose_short = purpose_short[:57] + "..."
        index_rows.append(
            f"| [{name}]({name}.md) | {purpose_short} | {'✓' if report.healthy else '✗'} | "
            f"{report.node_count} | {report.edge_count} | {len(report.interrupt_nodes)} | "
            f"{len(report.parallel_branches)} | {report.graph_depth} |"
        )

    index_rows.append("")
    index_rows.append(
        "_Note: `product` and `docs` departments have no LangGraph workflow of their own "
        "(confirmed in services/integration/lifecycle.py's `_graph_registry()` docstring) and are "
        "intentionally absent from this index rather than represented with an invented graph. "
        "`manager` is split into two independent graphs — `manager_lifecycle` and "
        "`manager_delegation` — rather than a single `manager` entry._"
    )
    index_path = out / "workflow_index.md"
    index_path.write_text("\n".join(index_rows) + "\n")
    written.append(str(index_path))
    return written
