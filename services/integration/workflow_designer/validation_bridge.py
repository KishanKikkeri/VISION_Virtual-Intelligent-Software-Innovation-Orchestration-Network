"""
services/integration/workflow_designer/validation_bridge.py
=================================
M4.8 §7 Validation + §8 Version Integration (diff/restore half) + §9
Replay Overlay (data-fetch half). "Bridge directly into: Workflow
Validator / Graph Linter / Version Registry. Never duplicate validation
logic." — every function here tries a real platform module first and
only performs the smallest possible designer-local check when that
import doesn't resolve, exactly mirroring the graceful-degradation
convention `plugin_repository.fetch_plugin_dashboard_section` and every
prior M4.x package's optional-integration functions established.

**Scope note (see docs/M4.8_Workflow_Designer_Handover.md §3):** this
sandbox slice does not include real `workflow_validator.py`,
`graph_linter.py`, `version_registry.py`, or `replay_engine.py` modules
to import. Every bridge function below documents its own real-import
attempt and its fallback so wiring the real modules later is a matter of
confirming the import path/call signature, not rewriting this module.
"""
from __future__ import annotations

import importlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from services.integration.workflow_designer.designer_models import (
    LayoutDiffResult, ReplayNodeState, ReplayOverlay, ValidationBridgeIssue, ValidationBridgeResult,
    VersionDiffEntry, WorkflowLayout,
)
from services.integration.workflow_designer.graph_builder import build_graph


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _try_import(module_path: str, attr: str):
    try:
        module = importlib.import_module(module_path)
        return getattr(module, attr)
    except Exception:  # noqa: BLE001 — module/attr not present in this sandbox slice
        return None


# ── §7 Validation ────────────────────────────────────────────────────────

def _local_structural_checks(layout: WorkflowLayout) -> List[ValidationBridgeIssue]:
    """The minimal, designer-local checks this module performs itself
    (not delegated to an external validator, because they concern the
    *canvas* — duplicate node ids, an edge to a nonexistent node — not
    runtime graph semantics an external `workflow_validator`/
    `graph_linter` would own)."""
    issues: List[ValidationBridgeIssue] = []
    seen: Dict[str, int] = {}
    for n in layout.nodes:
        seen[n.id] = seen.get(n.id, 0) + 1
    for node_id, count in seen.items():
        if count > 1:
            issues.append(ValidationBridgeIssue(rule_id="DESIGNER-DUPLICATE-NODE-ID", severity="error",
                                                  node_id=node_id, source="designer",
                                                  message=f"node id {node_id!r} appears {count} times"))

    build_result = build_graph(layout)
    for warning in build_result.warnings:
        issues.append(ValidationBridgeIssue(rule_id="DESIGNER-GRAPH-WARNING", severity="warning",
                                             message=warning, source="designer"))
    if not layout.nodes:
        issues.append(ValidationBridgeIssue(rule_id="DESIGNER-EMPTY-WORKFLOW", severity="warning",
                                             message="workflow has no nodes", source="designer"))
    return issues


def validate_layout(layout: WorkflowLayout) -> ValidationBridgeResult:
    """§7 entry point — folds designer-local structural checks together
    with whichever of `workflow_validator.validate_graph`/
    `graph_linter.lint_graph` resolves in the running platform. Each
    external check runs against the same `GraphBuildResult` shape
    `graph_builder.build_graph` already produces (never a second,
    duplicated notion of "graph"), and a missing external module simply
    marks that source `False` in `sources_available` rather than
    failing the whole validation call."""
    issues = list(_local_structural_checks(layout))
    sources_available = {"workflow_validator": False, "graph_linter": False}

    build_result = build_graph(layout)
    graph_payload = {"nodes": build_result.nodes, "edges": build_result.edges,
                      "entry_point": build_result.entry_point}

    validate_graph = _try_import("services.workflow.workflow_validator", "validate_graph")
    if validate_graph is not None:
        sources_available["workflow_validator"] = True
        try:
            for item in (validate_graph(graph_payload) or []):
                issues.append(ValidationBridgeIssue(
                    rule_id=getattr(item, "rule_id", "WORKFLOW-VALIDATOR"),
                    severity=getattr(item, "severity", "error"), message=str(getattr(item, "message", item)),
                    source="workflow_validator",
                ))
        except Exception as e:  # noqa: BLE001 — external validator present but raised; surface, don't crash
            issues.append(ValidationBridgeIssue(rule_id="WORKFLOW-VALIDATOR-ERROR", severity="warning",
                                                  message=f"workflow_validator raised: {e}", source="workflow_validator"))

    lint_graph = _try_import("services.workflow.graph_linter", "lint_graph")
    if lint_graph is not None:
        sources_available["graph_linter"] = True
        try:
            for item in (lint_graph(graph_payload) or []):
                issues.append(ValidationBridgeIssue(
                    rule_id=getattr(item, "rule_id", "GRAPH-LINTER"),
                    severity=getattr(item, "severity", "warning"), message=str(getattr(item, "message", item)),
                    source="graph_linter",
                ))
        except Exception as e:  # noqa: BLE001
            issues.append(ValidationBridgeIssue(rule_id="GRAPH-LINTER-ERROR", severity="warning",
                                                  message=f"graph_linter raised: {e}", source="graph_linter"))

    valid = not any(i.severity == "error" for i in issues)
    return ValidationBridgeResult(workflow_name=layout.workflow_name, valid=valid, issues=issues,
                                   sources_available=sources_available)


# ── §8 Version Integration ──────────────────────────────────────────────

def diff_layouts(old: WorkflowLayout, new: WorkflowLayout) -> LayoutDiffResult:
    """§8 "View diffs" — a pure, designer-local layout diff (node/edge
    add/remove + pure-position moves). Distinct from a runtime graph
    diff a real `version_registry`/`graph_diff` module would compute —
    this function never claims to replace that; `fetch_version_history`
    below is where a real `version_registry` import is attempted for the
    runtime-structural comparison."""
    entries: List[VersionDiffEntry] = []
    old_nodes = {n.id: n for n in old.nodes}
    new_nodes = {n.id: n for n in new.nodes}

    for node_id in sorted(set(new_nodes) - set(old_nodes)):
        entries.append(VersionDiffEntry(kind="node_added", node_id=node_id,
                                         detail=f"node {node_id!r} added"))
    for node_id in sorted(set(old_nodes) - set(new_nodes)):
        entries.append(VersionDiffEntry(kind="node_removed", node_id=node_id,
                                         detail=f"node {node_id!r} removed"))
    for node_id in sorted(set(old_nodes) & set(new_nodes)):
        o, n = old_nodes[node_id], new_nodes[node_id]
        if (o.x, o.y) != (n.x, n.y):
            entries.append(VersionDiffEntry(kind="node_moved", node_id=node_id,
                                             detail=f"moved from ({o.x},{o.y}) to ({n.x},{n.y})"))
        if o.node_type != n.node_type or o.config != n.config:
            entries.append(VersionDiffEntry(kind="node_changed", node_id=node_id,
                                             detail=f"node {node_id!r} type/config changed"))

    old_edges = {e.id: e for e in old.edges}
    new_edges = {e.id: e for e in new.edges}
    for edge_id in sorted(set(new_edges) - set(old_edges)):
        entries.append(VersionDiffEntry(kind="edge_added", edge_id=edge_id,
                                         detail=f"edge {edge_id!r} added"))
    for edge_id in sorted(set(old_edges) - set(new_edges)):
        entries.append(VersionDiffEntry(kind="edge_removed", edge_id=edge_id,
                                         detail=f"edge {edge_id!r} removed"))

    return LayoutDiffResult(workflow_name=new.workflow_name, from_version=old.version, to_version=new.version,
                             entries=entries)


def fetch_version_history(workflow_name: str, db: Any = None) -> Optional[List[Dict[str, Any]]]:
    """§8 "Compare against previous versions" — tries M4.1's real
    `version_registry.VersionRegistry.list_versions` (not present in this
    sandbox slice — see module docstring); returns `None` (not `[]`) when
    that integration is unavailable, so a caller can distinguish "no
    version registry wired" from "workflow has zero prior versions"."""
    list_versions = _try_import("services.workflow.version_registry", "list_versions")
    if list_versions is None:
        return None
    try:
        result = list_versions(workflow_name, db) if db is not None else list_versions(workflow_name)
        return list(result) if result is not None else []
    except Exception:  # noqa: BLE001
        return None


# ── §9 Replay Overlay ────────────────────────────────────────────────────

def fetch_replay_overlay(workflow_name: str, execution_id: str, db: Any = None) -> ReplayOverlay:
    """§9 entry point — tries M4.2's real `replay_engine.get_execution_trace`
    (not present in this sandbox slice — see module docstring). When that
    import doesn't resolve, returns an `available=False` overlay (an
    explicit, typed "no replay data" rather than raising) so the SPA can
    render "replay unavailable" instead of crashing (§9 "Designer remains
    read-only during replay mode" implies replay mode itself must degrade
    gracefully, not the whole designer view)."""
    get_execution_trace = _try_import("services.workflow.replay_engine", "get_execution_trace")
    if get_execution_trace is None:
        return ReplayOverlay(workflow_name=workflow_name, execution_id=execution_id, available=False)
    try:
        trace = get_execution_trace(execution_id, db) if db is not None else get_execution_trace(execution_id)
    except Exception:  # noqa: BLE001
        return ReplayOverlay(workflow_name=workflow_name, execution_id=execution_id, available=False)
    if trace is None:
        return ReplayOverlay(workflow_name=workflow_name, execution_id=execution_id, available=False)

    node_states = [
        ReplayNodeState(node_id=getattr(s, "node_id", s.get("node_id") if isinstance(s, dict) else ""),
                        status=getattr(s, "status", s.get("status", "pending") if isinstance(s, dict) else "pending"),
                        duration_ms=getattr(s, "duration_ms", s.get("duration_ms") if isinstance(s, dict) else None),
                        started_at=getattr(s, "started_at", s.get("started_at") if isinstance(s, dict) else None),
                        finished_at=getattr(s, "finished_at", s.get("finished_at") if isinstance(s, dict) else None))
        for s in getattr(trace, "node_states", trace.get("node_states", []) if isinstance(trace, dict) else [])
    ]
    current = next((s.node_id for s in node_states if s.status == "running"), None)
    failed = next((s.node_id for s in node_states if s.status == "failed"), None)
    return ReplayOverlay(workflow_name=workflow_name, execution_id=execution_id, current_node_id=current,
                          failed_node_id=failed, node_states=node_states, available=True)
