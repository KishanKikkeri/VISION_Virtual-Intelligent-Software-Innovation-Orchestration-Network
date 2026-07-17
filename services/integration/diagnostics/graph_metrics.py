"""
services/integration/diagnostics/graph_metrics.py
=================================
M3.10 §5 Graph Metrics — reusable, pure static-analysis utilities over
a graph's plain node/edge representation. Deliberately framework-free
(no LangGraph imports): every function here takes `nodes: Set[str]`
and `edges: List[Tuple[str, str]]` so it can be unit-tested in
isolation and reused by both `workflow_validator` (diagnostics) and
`graph_exporter` (Mermaid/docs generation) without either depending on
the other.

All functions are total (never raise) and degrade gracefully on the
empty graph — `graph_depth(set(), [])` is `0`, not an exception.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Dict, List, Set, Tuple

START = "__start__"
END = "__end__"

Edge = Tuple[str, str]


def build_adjacency(edges: List[Edge]) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    """Returns (forward_adjacency, reverse_adjacency)."""
    adj: Dict[str, List[str]] = defaultdict(list)
    radj: Dict[str, List[str]] = defaultdict(list)
    for s, t in edges:
        adj[s].append(t)
        radj[t].append(s)
    return adj, radj


def bfs_reachable(adj: Dict[str, List[str]], start: str) -> Set[str]:
    """All nodes reachable from `start`, inclusive of `start` itself."""
    if start not in adj and not any(start == s or start in targets for s, targets in adj.items()):
        # start may be a valid but edge-less node (e.g. an isolated node);
        # still counts as reachable from itself.
        pass
    seen = {start}
    q = deque([start])
    while q:
        n = q.popleft()
        for nxt in adj.get(n, []):
            if nxt not in seen:
                seen.add(nxt)
                q.append(nxt)
    return seen


def isolated_nodes(nodes: Set[str], edges: List[Edge]) -> List[str]:
    """Nodes with neither an incoming nor an outgoing edge. START/END are
    never considered isolated even if a graph is otherwise empty."""
    touched: Set[str] = set()
    for s, t in edges:
        touched.add(s)
        touched.add(t)
    return sorted(n for n in nodes if n not in touched and n not in (START, END))


def detect_cycles(nodes: Set[str], edges: List[Edge]) -> List[List[str]]:
    """Enumerates simple cycles via DFS with a recursion-stack path,
    de-duplicated by their frozenset of participating nodes (so A->B->A
    and the same cycle found from a different entry node are reported
    once). This is a static-structure check — it does not know whether
    a LangGraph conditional edge would ever *actually* take the looping
    branch at runtime, only that the edge exists in the compiled graph."""
    adj, _ = build_adjacency(edges)
    cycles: List[List[str]] = []
    seen_cycle_keys: Set[frozenset] = set()

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in nodes}
    path: List[str] = []

    def dfs(node: str) -> None:
        color[node] = GRAY
        path.append(node)
        for nxt in adj.get(node, []):
            if nxt not in color:
                continue
            if color[nxt] == GRAY:
                # Found a back-edge -> cycle is path[idx of nxt:] + [nxt]
                try:
                    idx = path.index(nxt)
                except ValueError:
                    idx = 0
                cycle = path[idx:]
                key = frozenset(cycle)
                if key not in seen_cycle_keys:
                    seen_cycle_keys.add(key)
                    cycles.append(cycle)
            elif color[nxt] == WHITE:
                dfs(nxt)
        path.pop()
        color[node] = BLACK

    for n in sorted(nodes):
        if color.get(n) == WHITE:
            dfs(n)
    return cycles


def graph_depth(nodes: Set[str], edges: List[Edge], start: str = START, end: str = END) -> int:
    """Longest simple path (edge count) from `start` to `end`. Back-edges
    identified by `detect_cycles` are excluded so a self-loop or a
    revise/retry cycle doesn't produce an unbounded depth — this
    reports the longest *acyclic* path, i.e. "how many stages could a
    single pass through this workflow visit at most"."""
    if start not in nodes or end not in nodes:
        return 0
    cycle_nodes: Set[str] = set()
    for c in detect_cycles(nodes, edges):
        cycle_nodes.update(c)
    # Drop edges whose target is a node already seen earlier in that
    # same cycle set feeding back on itself (back-edges only, not the
    # whole cycle) — approximate by dropping self-referential/cycle
    # re-entry edges: an edge (s, t) is a back-edge if both endpoints
    # are in the same detected cycle AND t has already got other
    # forward in-edges from outside the cycle (heuristic kept simple:
    # just drop edges where t == s, and edges that close a cycle back
    # to a node already earlier reached in DFS order below).
    adj, _ = build_adjacency(edges)

    memo: Dict[str, int] = {}

    def longest_from(node: str, visiting: frozenset) -> int:
        if node == end:
            return 0
        if node in visiting:
            return float("-inf")  # cyclic re-entry — not a valid acyclic path
        key = node
        if key in memo:
            return memo[key]
        best = float("-inf")
        for nxt in adj.get(node, []):
            sub = longest_from(nxt, visiting | {node})
            if sub != float("-inf"):
                best = max(best, 1 + sub)
        memo[key] = best
        return best

    depth = longest_from(start, frozenset())
    return depth if depth != float("-inf") else 0


def average_branching_factor(nodes: Set[str], edges: List[Edge]) -> float:
    """Mean out-degree across nodes that have at least one outgoing edge
    (nodes with zero out-edges, e.g. END, don't count as "branching")."""
    adj, _ = build_adjacency(edges)
    out_degrees = [len(v) for v in adj.values() if v]
    if not out_degrees:
        return 0.0
    return round(sum(out_degrees) / len(out_degrees), 2)


def dead_end_count(nodes: Set[str], edges: List[Edge], end: str = END) -> int:
    """Nodes other than `end` with zero outgoing edges — a structural
    dead end (no declared route back out, not even to END)."""
    adj, _ = build_adjacency(edges)
    return sum(1 for n in nodes if n != end and not adj.get(n))


def reachability_percentage(nodes: Set[str], edges: List[Edge], start: str = START) -> float:
    """% of nodes reachable from `start`. 100.0 on an empty graph (there's
    nothing to fail to reach)."""
    if not nodes:
        return 100.0
    if start not in nodes:
        return 0.0
    adj, _ = build_adjacency(edges)
    reached = bfs_reachable(adj, start)
    return round(100.0 * len(reached & nodes) / len(nodes), 1)


def conditional_node_count(edges_with_flag: List[Tuple[str, str, bool]]) -> int:
    """Count of distinct source nodes that have at least one conditional
    outgoing edge."""
    return len({s for s, _t, cond in edges_with_flag if cond})
