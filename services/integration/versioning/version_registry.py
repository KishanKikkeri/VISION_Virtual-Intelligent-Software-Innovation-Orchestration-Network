"""
services/integration/versioning/version_registry.py
===================================================
M4.1 Workflow Versioning — in-memory registry of workflow structural
snapshots (nodes, edges, conditional routes, signature) and version
evolution tracking.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from services.integration.validators import workflow_validator


class VersionDiff(BaseModel):
    from_version: str
    to_version: str
    added_nodes: List[str] = Field(default_factory=list)
    removed_nodes: List[str] = Field(default_factory=list)
    is_breaking: bool = False


class WorkflowVersionRecord(BaseModel):
    workflow: str
    version: str
    signature: str
    nodes: List[str] = Field(default_factory=list)
    edges: List[Any] = Field(default_factory=list)
    routes: Dict[str, Any] = Field(default_factory=dict)
    is_breaking_from_previous: bool = False
    diff_from_previous: Optional[VersionDiff] = None
    notes: Optional[str] = None


class VersionRegistry:
    def __init__(self) -> None:
        self._history: Dict[str, List[WorkflowVersionRecord]] = {}

    def list_versions(self, workflow: str) -> List[WorkflowVersionRecord]:
        return list(self._history.get(workflow, []))

    def get(self, workflow: str, version: str) -> Optional[WorkflowVersionRecord]:
        for rec in self._history.get(workflow, []):
            if rec.version == version:
                return rec
        return None

    def diff_versions(
        self, workflow: str, from_version: str, to_version: str
    ) -> Optional[VersionDiff]:
        v_from = self.get(workflow, from_version)
        v_to = self.get(workflow, to_version)
        if v_from is None or v_to is None:
            return None
        from_nodes = set(v_from.nodes)
        to_nodes = set(v_to.nodes)
        added = sorted(to_nodes - from_nodes)
        removed = sorted(from_nodes - to_nodes)
        is_breaking = bool(removed)
        return VersionDiff(
            from_version=from_version,
            to_version=to_version,
            added_nodes=added,
            removed_nodes=removed,
            is_breaking=is_breaking,
        )

    def register(
        self,
        workflow: str,
        nodes: List[str],
        edges: Optional[List[Any]] = None,
        routes: Optional[Dict[str, Any]] = None,
        notes: Optional[str] = None,
    ) -> WorkflowVersionRecord:
        edges = edges or []
        routes = routes or {}
        raw = f"{workflow}:{sorted(nodes)}:{sorted(routes.keys())}"
        signature = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

        history = self._history.setdefault(workflow, [])
        if history:
            latest = history[-1]
            if latest.signature == signature:
                return latest
            prev_nodes = set(latest.nodes)
            cur_nodes = set(nodes)
            removed = sorted(prev_nodes - cur_nodes)
            added = sorted(cur_nodes - prev_nodes)
            is_breaking = bool(removed)

            try:
                parts = latest.version.split(".")
                major = int(parts[0])
                minor = int(parts[1]) if len(parts) > 1 else 0
            except ValueError:
                major = 1
                minor = 0

            if is_breaking:
                version_str = f"{major + 1}.0"
            else:
                version_str = f"{major}.{minor + 1}"

            diff = VersionDiff(
                from_version=latest.version,
                to_version=version_str,
                added_nodes=added,
                removed_nodes=removed,
                is_breaking=is_breaking,
            )
        else:
            version_str = "1.0"
            is_breaking = False
            diff = None

        record = WorkflowVersionRecord(
            workflow=workflow,
            version=version_str,
            signature=signature,
            nodes=nodes,
            edges=edges,
            routes=routes,
            is_breaking_from_previous=is_breaking,
            diff_from_previous=diff,
            notes=notes,
        )
        history.append(record)
        return record


default_registry = VersionRegistry()


def register_current_version(
    workflow: str,
    registry: Optional[VersionRegistry] = None,
    notes: Optional[str] = None,
) -> WorkflowVersionRecord:
    report = workflow_validator.get_workflow_report(workflow)
    if report is None:
        raise ValueError(f"unknown workflow {repr(workflow)}")
    reg = registry or default_registry
    nodes = list(report.reachable_nodes)
    routes = {r.source: r.outcomes for r in report.conditional_routes}
    return reg.register(
        workflow=workflow,
        nodes=nodes,
        edges=[],
        routes=routes,
        notes=notes,
    )
