"""
services/integration/lifecycle.py
=================================
Workflow validation (spec §1 Full Lifecycle Validation + §7 Workflow
Validation): every LangGraph in the platform actually builds, every
node is reachable from `__start__`, every node can reach `__end__`
(no dead ends / no cyclic deadlocks), and every routing function
referenced by name in a graph module actually exists.

**Known limitation, stated plainly**: reachability is computed from
`CompiledGraph.get_graph()`'s static edge list. LangGraph's `Send()`
API (used for dynamic fan-out — see services/architecture/workflows/
architecture_graph.py's `parallel_design` node) dispatches to nodes at
*runtime*, which do not appear as static edges. A naive static-only
analysis would therefore flag every Send()-dispatched node as
"unreachable", which is a false positive, not a real bug (confirmed by
reading architecture_graph.py's source directly). `_is_likely_send_target()`
below is a best-effort heuristic — if the module's source contains
`Send(` AND the candidate node's name (or `f"{name}_node"`) appears as
a quoted string literal elsewhere in the same source file, it's
downgraded from "unreachable" (a failure) to "dynamically dispatched,
not statically verifiable" (informational, not a failure). This is
explicitly a heuristic, not a proof — documented here and in
docs/M3.9_Platform_Integration_Handover.md so nobody mistakes a clean
report for a formal verification.
"""
from __future__ import annotations

import inspect
import re
from collections import defaultdict, deque
from typing import Any, Callable, Dict, List, Optional, Set

from pydantic import BaseModel, Field

START = "__start__"
END = "__end__"


class GraphAnalysis(BaseModel):
    name: str
    built: bool
    error: Optional[str] = None
    node_count: int = 0
    edge_count: int = 0
    unreachable_nodes: List[str] = Field(default_factory=list)
    dead_end_nodes: List[str] = Field(default_factory=list)
    likely_dynamic_dispatch_nodes: List[str] = Field(default_factory=list)
    end_reachable: bool = False
    passed: bool = False


def _bfs(adj: Dict[str, List[str]], start: str) -> Set[str]:
    seen = {start}
    q = deque([start])
    while q:
        n = q.popleft()
        for nxt in adj.get(n, []):
            if nxt not in seen:
                seen.add(nxt)
                q.append(nxt)
    return seen


def _is_likely_send_target(node: str, source: str) -> bool:
    if "Send(" not in source:
        return False
    return f'"{node}"' in source or f"'{node}'" in source \
        or f'"{node}_node"' in source or f"'{node}_node'" in source


def analyze_graph(name: str, builder: Callable[..., Any], builder_kwargs: Optional[Dict[str, Any]] = None,
                   source_module: Any = None) -> GraphAnalysis:
    """Builds `builder(**builder_kwargs)` and analyzes the resulting
    compiled LangGraph's static structure. Never raises — a build
    failure is itself a (failed) GraphAnalysis, not an exception."""
    builder_kwargs = builder_kwargs or {}
    try:
        compiled = builder(**builder_kwargs)
    except Exception as e:
        return GraphAnalysis(name=name, built=False, error=str(e), passed=False)

    try:
        gg = compiled.get_graph()
        nodes = set(gg.nodes.keys())
        edges = [(e.source, e.target) for e in gg.edges]
    except Exception as e:
        return GraphAnalysis(name=name, built=True, error=f"graph introspection failed: {e}", passed=False)

    adj: Dict[str, List[str]] = defaultdict(list)
    radj: Dict[str, List[str]] = defaultdict(list)
    for s, t in edges:
        adj[s].append(t)
        radj[t].append(s)

    reach_from_start = _bfs(adj, START) if START in nodes else set()
    can_reach_end = _bfs(radj, END) if END in nodes else set()

    unreachable = nodes - reach_from_start
    dead_end = (nodes - {END}) - can_reach_end

    likely_dynamic: List[str] = []
    if (unreachable or dead_end) and source_module is not None:
        try:
            src = inspect.getsource(source_module)
        except Exception:
            src = ""
        candidates = unreachable | dead_end
        likely_dynamic = sorted(n for n in candidates if _is_likely_send_target(n, src))

    real_unreachable = sorted(unreachable - set(likely_dynamic))
    real_dead_end = sorted(dead_end - set(likely_dynamic))

    passed = not real_unreachable and not real_dead_end and (END in nodes) and (END in reach_from_start or not nodes)
    return GraphAnalysis(
        name=name, built=True, node_count=len(nodes), edge_count=len(edges),
        unreachable_nodes=real_unreachable, dead_end_nodes=real_dead_end,
        likely_dynamic_dispatch_nodes=likely_dynamic,
        end_reachable=END in reach_from_start, passed=passed,
    )


def _graph_registry() -> List[Dict[str, Any]]:
    """Lazily imported so that a missing/broken department module
    doesn't prevent importing this validator module itself."""
    registry: List[Dict[str, Any]] = []

    def _try(name: str, import_path: str, builder_name: str, kwargs: Dict[str, Any] = None):
        try:
            module = __import__(import_path, fromlist=[builder_name])
            builder = getattr(module, builder_name)
            registry.append({"name": name, "builder": builder, "kwargs": kwargs or {}, "module": module})
        except Exception as e:
            registry.append({"name": name, "builder": None, "kwargs": {}, "module": None, "import_error": str(e)})

    _try("architecture", "services.architecture.workflows.architecture_graph", "build_architecture_graph")
    _try("devops", "services.devops.workflows.devops_graph", "build_devops_graph")
    _try("engineering", "services.engineering.workflows.engineering_graph", "build_engineering_graph")
    _try("security", "services.security.workflows.security_graph", "build_security_graph")
    _try("qa", "services.qa.workflows.qa_graph", "build_qa_graph")
    _try("monitoring", "services.monitoring.workflows.monitoring_graph", "build_monitoring_graph",
         {"factory": None})
    _try("incident_response", "services.incident_response.workflows.incident_response_graph",
         "build_incident_response_graph", {"factory": None})
    _try("manager_lifecycle", "services.manager.graphs.lifecycle", "build_lifecycle_graph")
    _try("manager_delegation", "services.manager.graphs.delegation", "build_delegation_graph")

    try:
        from services.repository.managers import RepositoryDeps
        deps = RepositoryDeps(db_factory=lambda: None, provider=None)
        _try("repository", "services.repository.workflows.repository_graph", "build_repository_graph",
             {"deps": deps})
    except Exception as e:
        registry.append({"name": "repository", "builder": None, "kwargs": {}, "module": None,
                          "import_error": str(e)})

    return registry


def validate_all_workflows() -> Dict[str, GraphAnalysis]:
    """Every LangGraph in the platform — spec §7. `product` and `docs`
    have no LangGraph of their own (confirmed during reconnaissance —
    no services/product/workflows or services/docs/workflows package
    exists); they are intentionally absent from this report rather
    than silently invented."""
    results: Dict[str, GraphAnalysis] = {}
    for entry in _graph_registry():
        if entry.get("builder") is None:
            results[entry["name"]] = GraphAnalysis(
                name=entry["name"], built=False,
                error=entry.get("import_error", "unknown import error"), passed=False,
            )
            continue
        results[entry["name"]] = analyze_graph(
            entry["name"], entry["builder"], entry["kwargs"], source_module=entry["module"])
    return results


def validate_routing_functions_exist(module_import_path: str, function_names: List[str]) -> Dict[str, bool]:
    """Spec §7 'Every routing function exists'. Given a workflow module's
    import path and a list of function names it's expected to define
    (e.g. from a routing.py sibling), confirms each is an importable
    callable."""
    try:
        module = __import__(module_import_path, fromlist=function_names)
    except Exception:
        return {name: False for name in function_names}
    return {name: callable(getattr(module, name, None)) for name in function_names}
