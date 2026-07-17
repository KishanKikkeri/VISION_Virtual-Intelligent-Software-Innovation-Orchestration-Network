"""
services/integration/validators/workflow_validator.py
=================================
M3.10 §1 Workflow Validator — a complete graph analyzer, one level up
from services/integration/lifecycle.py's `GraphAnalysis` (which only
answers "does it build, is everything reachable, can everything reach
END" for the startup gate). `WorkflowReport` here additionally surfaces
entry/finish nodes, isolated nodes, cycles, interrupt/checkpoint
nodes, the conditional-routing table, parallel Send() branches, and
graph depth — everything the new `/platform/workflows*` diagnostics
endpoints and the Mermaid/docs generator need.

Deliberately does **not** replace `lifecycle.GraphAnalysis` — that
model is load-bearing for `orchestrator.compute_readiness()` and the
existing `/platform/report` persistence path, and per this milestone's
constraints this is meant to be additive. `_graph_registry()` (the
list of workflow name -> builder -> module) is imported from
`lifecycle` rather than duplicated, so there is exactly one place that
knows how to construct each of the platform's 10 LangGraphs.
"""
from __future__ import annotations

import inspect
import re
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

from services.integration import lifecycle
from services.integration.diagnostics import graph_metrics

START = graph_metrics.START
END = graph_metrics.END


class ConditionalRoute(BaseModel):
    source: str
    function: Optional[str] = None
    outcomes: Dict[str, str] = Field(default_factory=dict)


class ParallelBranch(BaseModel):
    fan_out_node: str
    kind: str  # "static_edges" | "send"
    targets: List[str] = Field(default_factory=list)


class WorkflowReport(BaseModel):
    workflow: str
    healthy: bool
    built: bool
    error: Optional[str] = None

    node_count: int = 0
    edge_count: int = 0
    entry_node: Optional[str] = None
    finish_node: Optional[str] = None

    reachable_nodes: List[str] = Field(default_factory=list)
    unreachable_nodes: List[str] = Field(default_factory=list)
    isolated_nodes: List[str] = Field(default_factory=list)
    cycles: List[List[str]] = Field(default_factory=list)

    interrupt_nodes: List[str] = Field(default_factory=list)
    checkpoint_capable: bool = False
    checkpoint_nodes: List[str] = Field(default_factory=list)

    conditional_routes: List[ConditionalRoute] = Field(default_factory=list)
    parallel_branches: List[ParallelBranch] = Field(default_factory=list)

    graph_depth: int = 0
    average_branching_factor: float = 0.0
    dead_end_count: int = 0
    reachability_pct: float = 100.0

    warnings: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)


_CONDITIONAL_EDGES_RE = re.compile(
    r"""add_conditional_edges\(\s*["'](?P<source>[^"']+)["']\s*,\s*(?P<func>[A-Za-z_][A-Za-z0-9_]*)\s*,\s*\{(?P<body>[^}]*)\}""",  # noqa: E501
    re.DOTALL,
)
_KV_RE = re.compile(r"""["'](?P<key>[^"']+)["']\s*:\s*["'](?P<val>[^"']+)["']""")
_FAN_OUT_STATIC_RE = re.compile(r"""add_edge\(\s*["'](?P<source>[^"']+)["']\s*,\s*["'](?P<target>[^"']+)["']\s*\)""")
_INTERRUPT_LIST_RE = re.compile(
    r"""(?P<kind>interrupt_before|interrupt_after)["']?\s*[:=]\s*\[(?P<body>[^\]]*)\]""")
_QUOTED_RE = re.compile(r"""["']([^"']+)["']""")


def _extract_conditional_routes(source: str) -> List[ConditionalRoute]:
    routes: List[ConditionalRoute] = []
    for m in _CONDITIONAL_EDGES_RE.finditer(source):
        outcomes = {k: v for k, v in _KV_RE.findall(m.group("body"))}
        routes.append(ConditionalRoute(source=m.group("source"), function=m.group("func"), outcomes=outcomes))
    return routes


def _extract_static_fan_out(source: str) -> Dict[str, List[str]]:
    """Groups plain `g.add_edge(src, tgt)` calls by source node — used to
    detect static (non-Send) parallel fan-out: a source node with 2+
    unconditional outgoing edges to sibling nodes that are never each
    other's target."""
    fan_out: Dict[str, List[str]] = {}
    for m in _FAN_OUT_STATIC_RE.finditer(source):
        fan_out.setdefault(m.group("source"), []).append(m.group("target"))
    return fan_out


_DEF_RE = re.compile(r"^(?:async def|def) (\w+)\(", re.MULTILINE)


def _function_bodies(source: str) -> Dict[str, str]:
    """Splits a module's source into {function_name: body_text} by
    top-level `def`/`async def` boundaries (best-effort — good enough
    for the fixed, consistently-formatted graph modules in this
    codebase; not a general Python parser)."""
    matches = list(_DEF_RE.finditer(source))
    bodies: Dict[str, str] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(source)
        bodies[m.group(1)] = source[m.start():end]
    return bodies


def _extract_send_fan_out(source: str, nodes: Set[str]) -> Dict[str, List[str]]:
    """For every top-level function whose body calls `Send(`, reuses
    lifecycle._is_likely_send_target's proven node-matching heuristic
    (rather than a raw character window, which over- or under-matches)
    to find which registered node names that function's Send() calls
    are plausibly dispatching to."""
    if "Send(" not in source:
        return {}
    result: Dict[str, List[str]] = {}
    for fn_name, raw_body in _function_bodies(source).items():
        # Strip docstrings/comments so prose that merely *mentions*
        # "Send()" (common in these modules' module/function docstrings)
        # isn't mistaken for an actual dispatch call.
        body = re.sub(r'""".*?"""', "", raw_body, flags=re.DOTALL)
        body = "\n".join(line.split("#", 1)[0] for line in body.splitlines())
        if "Send(" not in body:
            continue
        targets = sorted(n for n in nodes if lifecycle._is_likely_send_target(n, body))  # noqa: SLF001
        if targets:
            result[fn_name] = targets
    return result


def _extract_interrupt_nodes_from_source(source: str) -> Dict[str, List[str]]:
    found: Dict[str, List[str]] = {"interrupt_before": [], "interrupt_after": []}
    for m in _INTERRUPT_LIST_RE.finditer(source):
        found[m.group("kind")] = _QUOTED_RE.findall(m.group("body"))
    return found


def analyze_workflow(
    name: str,
    builder: Optional[Callable[..., Any]],
    builder_kwargs: Optional[Dict[str, Any]] = None,
    source_module: Any = None,
    import_error: Optional[str] = None,
) -> WorkflowReport:
    """Never raises. A build failure produces an unhealthy WorkflowReport
    with the exception message in `errors`, not an exception bubbling
    up to the caller — diagnostics endpoints must be able to describe a
    broken workflow, not 500 on one."""
    if builder is None:
        return WorkflowReport(
            workflow=name, healthy=False, built=False,
            error=import_error or "workflow builder could not be imported",
            errors=[import_error or "workflow builder could not be imported"],
        )

    builder_kwargs = builder_kwargs or {}
    try:
        compiled = builder(**builder_kwargs)
    except Exception as e:  # noqa: BLE001
        return WorkflowReport(workflow=name, healthy=False, built=False, error=str(e), errors=[str(e)])

    warnings: List[str] = []
    errors: List[str] = []

    try:
        gg = compiled.get_graph()
        nodes: Set[str] = set(gg.nodes.keys())
        edges_with_flag: List[Tuple[str, str, bool]] = [
            (e.source, e.target, bool(getattr(e, "conditional", False))) for e in gg.edges
        ]
    except Exception as e:  # noqa: BLE001
        return WorkflowReport(
            workflow=name, healthy=False, built=True,
            error=f"graph introspection failed: {e}", errors=[f"graph introspection failed: {e}"],
        )

    edges: List[Tuple[str, str]] = [(s, t) for s, t, _c in edges_with_flag]
    adj, radj = graph_metrics.build_adjacency(edges)

    entry_node = next(iter(adj.get(START, [])), None) if START in nodes else None
    finish_node = END if END in nodes else None

    reachable = graph_metrics.bfs_reachable(adj, START) if START in nodes else set()
    unreachable = sorted(nodes - reachable)
    isolated = graph_metrics.isolated_nodes(nodes, edges)
    cycles = graph_metrics.detect_cycles(nodes, edges)

    # Nodes only reachable via a runtime Send() fan-out (see
    # lifecycle.py's module docstring) are a static-analysis false
    # positive, not a real bug — downgrade them from
    # unreachable/isolated to informational, exactly like lifecycle.py
    # does, so this richer report doesn't disagree with the startup
    # gate about which graphs are healthy.
    likely_dynamic: List[str] = []
    if (unreachable or isolated) and source_module is not None:
        try:
            _src_for_dynamic = inspect.getsource(source_module)
        except Exception:  # noqa: BLE001
            _src_for_dynamic = ""
        if _src_for_dynamic:
            candidates = set(unreachable) | set(isolated)
            likely_dynamic = sorted(n for n in candidates if lifecycle._is_likely_send_target(n, _src_for_dynamic))  # noqa: SLF001
    if likely_dynamic:
        unreachable = sorted(set(unreachable) - set(likely_dynamic))
        isolated = sorted(set(isolated) - set(likely_dynamic))

    # Interrupts / checkpointing — live introspection first (most
    # reliable: reads the *actual* compiled graph, not the source text),
    # falling back to a source-regex heuristic only for fields the
    # compiled object doesn't expose.
    interrupt_before = sorted(set(getattr(compiled, "interrupt_before_nodes", None) or []))
    interrupt_after = sorted(set(getattr(compiled, "interrupt_after_nodes", None) or []))
    interrupt_nodes = sorted(set(interrupt_before) | set(interrupt_after))
    checkpoint_capable = "checkpointer" in inspect.signature(builder).parameters
    # Checkpoint boundaries = the interrupt points LangGraph actually
    # exposes as meaningful pause/resume nodes when a checkpointer is
    # attached (every superstep is technically persisted, but these are
    # the ones a caller can deliberately resume at).
    checkpoint_nodes = list(interrupt_nodes)

    conditional_routes: List[ConditionalRoute] = []
    parallel_branches: List[ParallelBranch] = []
    src_text = ""
    if source_module is not None:
        try:
            src_text = inspect.getsource(source_module)
        except Exception:  # noqa: BLE001
            src_text = ""

    if src_text:
        conditional_routes = _extract_conditional_routes(src_text)

        send_fan_out = _extract_send_fan_out(src_text, nodes)
        for fn_node, targets in send_fan_out.items():
            parallel_branches.append(ParallelBranch(fan_out_node=fn_node, kind="send", targets=targets))

        static_fan_out = _extract_static_fan_out(src_text)
        for source_node, targets in static_fan_out.items():
            distinct = sorted(set(targets))
            if len(distinct) >= 2 and source_node not in {t for t in distinct}:
                parallel_branches.append(
                    ParallelBranch(fan_out_node=source_node, kind="static_edges", targets=distinct))

        if not interrupt_nodes:
            regex_interrupts = _extract_interrupt_nodes_from_source(src_text)
            all_regex = sorted(set(regex_interrupts["interrupt_before"]) | set(regex_interrupts["interrupt_after"]))
            if all_regex:
                # Compiled object disagreed with source text (e.g. this
                # particular build call didn't pass a checkpointer) —
                # surface as informational, not silently substituted.
                warnings.append(
                    f"source declares interrupt node(s) {all_regex} but the compiled graph "
                    f"(as built here) reports none — likely compiled without a checkpointer")

    if entry_node is None:
        errors.append("no entry point found from __start__")
    if finish_node is None:
        errors.append("graph has no __end__ node")
    elif END not in reachable and nodes:
        warnings.append("__end__ is not reachable from __start__")
    if unreachable:
        warnings.append(f"unreachable node(s): {unreachable}")
    if isolated:
        warnings.append(f"isolated node(s) with no edges at all: {isolated}")
    dead_ends = graph_metrics.dead_end_count(nodes, edges, end=END)
    dead_ends = max(0, dead_ends - len(likely_dynamic))
    if dead_ends:
        warnings.append(f"{dead_ends} node(s) have no outgoing route (structural dead end)")
    if likely_dynamic:
        warnings.append(
            f"node(s) {likely_dynamic} are only reachable via a runtime Send() fan-out — "
            f"not a static-analysis failure, see lifecycle.py")

    healthy = not errors and not unreachable and (finish_node is not None) and (END in reachable or not nodes)

    return WorkflowReport(
        workflow=name, healthy=healthy, built=True,
        node_count=len(nodes), edge_count=len(edges),
        entry_node=entry_node, finish_node=finish_node,
        reachable_nodes=sorted(reachable), unreachable_nodes=unreachable, isolated_nodes=isolated,
        cycles=[sorted(c) for c in cycles],
        interrupt_nodes=interrupt_nodes, checkpoint_capable=checkpoint_capable, checkpoint_nodes=checkpoint_nodes,
        conditional_routes=conditional_routes, parallel_branches=parallel_branches,
        graph_depth=graph_metrics.graph_depth(nodes, edges),
        average_branching_factor=graph_metrics.average_branching_factor(nodes, edges),
        dead_end_count=dead_ends,
        reachability_pct=graph_metrics.reachability_percentage(nodes, edges),
        warnings=warnings, errors=errors,
    )


def validate_all_workflows_detailed() -> Dict[str, WorkflowReport]:
    """One WorkflowReport per registered LangGraph — the M3.10 diagnostics
    equivalent of lifecycle.validate_all_workflows()."""
    results: Dict[str, WorkflowReport] = {}
    for entry in lifecycle._graph_registry():  # noqa: SLF001 (intentional reuse, see module docstring)
        results[entry["name"]] = analyze_workflow(
            entry["name"], entry.get("builder"), entry.get("kwargs"),
            source_module=entry.get("module"), import_error=entry.get("import_error"),
        )
    return results


def get_workflow_report(name: str) -> Optional[WorkflowReport]:
    return validate_all_workflows_detailed().get(name)
