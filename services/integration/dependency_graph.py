"""
services/integration/dependency_graph.py
=================================
Canonical department dependency graph for the VISION platform, and
deterministic validators over it (spec §1 Full Lifecycle Validation,
§2 Dependency Validation).

This is a **read-only, standalone model** of the pipeline — it does not
import or alter services/manager/graphs/lifecycle.py (Manager's own
W01 graph is not modified per this milestone's constraints). It was
built by reconciling three real sources found during reconnaissance:

  1. This milestone's own kickoff prompt (Product -> Architecture ->
     Engineering -> QA+Security -> DevOps -> Monitoring -> Incident
     Response).
  2. services/manager/graphs/lifecycle.py's actual phase sequence
     (intake -> requirements -> architecture -> project_structure ->
     implementation -> validation -> deployment -> monitoring ->
     complete), which fans QA+Security out as siblings, matching (1).
  3. services/manager/graphs/delegation.py's DEPARTMENT_HEAD_MAP,
     which additionally registers a `docs` department with no phase
     node of its own anywhere in Manager's lifecycle graph.

Reconciling these: `docs` is a real, registered department
(AGENT_REGISTRY has 7 docs agents, DEPARTMENT_HEAD_MAP has a
"docs_head" entry) that is **not part of the linear pipeline** this
module validates — it's intentionally excluded from
DEPARTMENT_DEPENDENCIES/PHASE_TIERS below and separately called out as
an orphan department (see ORPHAN_DEPARTMENTS and
docs/M3.9_Platform_Integration_Handover.md §Findings). This is reported
as a finding, not silently "fixed" by inventing a phase for it — Manager
is frozen per this milestone's constraints.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set

from pydantic import BaseModel, Field

# The 7 tiers of the canonical pipeline. Departments within the same
# tier run in parallel and do not depend on each other (QA and
# Security both depend only on Engineering, not on one another).
PHASE_TIERS: List[List[str]] = [
    ["product"],
    ["architecture"],
    ["engineering"],
    ["qa", "security"],
    ["devops"],
    ["monitoring"],
    ["incident_response"],
]

# department -> the set of departments that must have already
# completed (at least once) before this department may run.
DEPARTMENT_DEPENDENCIES: Dict[str, List[str]] = {
    "product": [],
    "architecture": ["product"],
    "engineering": ["architecture"],
    "qa": ["engineering"],
    "security": ["engineering"],
    "devops": ["qa", "security"],
    "monitoring": ["devops"],
    "incident_response": ["monitoring"],
}

# Registered departments (AGENT_REGISTRY) that are intentionally absent
# from the canonical pipeline above. See module docstring.
ORPHAN_DEPARTMENTS: Set[str] = {"docs", "manager"}

ALL_PIPELINE_DEPARTMENTS: List[str] = [d for tier in PHASE_TIERS for d in tier]


class DependencyCheckResult(BaseModel):
    department: str
    passed: bool
    missing: List[str] = Field(default_factory=list)
    reason: str = ""


class TransitionCheckResult(BaseModel):
    from_department: Optional[str]
    to_department: str
    passed: bool
    reason: str = ""


def required_dependencies_for(department: str) -> List[str]:
    return list(DEPARTMENT_DEPENDENCIES.get(department, []))


def validate_dependency(department: str, completed_departments: Set[str]) -> DependencyCheckResult:
    """Deterministic — spec §2: 'Return deterministic validation failures.'"""
    if department not in DEPARTMENT_DEPENDENCIES:
        return DependencyCheckResult(
            department=department, passed=False,
            reason=f"{department!r} is not a recognized pipeline department "
                    f"(known: {ALL_PIPELINE_DEPARTMENTS}; orphaned: {sorted(ORPHAN_DEPARTMENTS)})",
        )
    required = set(DEPARTMENT_DEPENDENCIES[department])
    missing = sorted(required - completed_departments)
    if missing:
        return DependencyCheckResult(
            department=department, passed=False, missing=missing,
            reason=f"{department} requires {sorted(required)} to have completed; missing {missing}",
        )
    return DependencyCheckResult(department=department, passed=True)


def validate_transition(from_department: Optional[str], to_department: str) -> TransitionCheckResult:
    """
    No skipped phases, no impossible transitions (spec §1). `from_department`
    is the department whose completion is triggering this transition (None
    for the very first phase). A transition is valid if `to_department`'s
    dependencies are satisfied by exactly {from_department} union whatever
    tier-mates are assumed already complete (this function checks the
    single-predecessor case; full completed-set checking is
    validate_dependency's job — this one is for adjacency/skip detection).
    """
    if to_department not in DEPARTMENT_DEPENDENCIES:
        return TransitionCheckResult(
            from_department=from_department, to_department=to_department, passed=False,
            reason=f"{to_department!r} is not a recognized pipeline department",
        )
    required = DEPARTMENT_DEPENDENCIES[to_department]
    if not required:
        # Entry phase (product) — only valid as the very first transition.
        if from_department is not None:
            return TransitionCheckResult(
                from_department=from_department, to_department=to_department, passed=False,
                reason=f"{to_department} has no dependencies; it can only be the pipeline's entry phase",
            )
        return TransitionCheckResult(from_department=from_department, to_department=to_department, passed=True)

    if from_department not in required:
        return TransitionCheckResult(
            from_department=from_department, to_department=to_department, passed=False,
            reason=f"Impossible/skipped transition: {to_department} requires one of {required}, "
                    f"got {from_department!r}",
        )
    return TransitionCheckResult(from_department=from_department, to_department=to_department, passed=True)


def topological_order() -> List[str]:
    """Flattens PHASE_TIERS in dependency-safe order."""
    return list(ALL_PIPELINE_DEPARTMENTS)


def has_cycle() -> bool:
    """Generic Kahn's-algorithm cycle check over DEPARTMENT_DEPENDENCIES —
    used by both dependency validation and, indirectly, workflow validation's
    'no cyclic deadlocks' requirement (spec §7) as a sanity check on the
    department graph itself (independent of any one LangGraph's own
    internal cycles, which lifecycle.py checks separately)."""
    indegree = {d: 0 for d in DEPARTMENT_DEPENDENCIES}
    for d, deps in DEPARTMENT_DEPENDENCIES.items():
        for _dep in deps:
            indegree[d] += 1
    # Build forward adjacency (dep -> dependents)
    forward: Dict[str, List[str]] = {d: [] for d in DEPARTMENT_DEPENDENCIES}
    for d, deps in DEPARTMENT_DEPENDENCIES.items():
        for dep in deps:
            forward.setdefault(dep, []).append(d)

    queue = [d for d, deg in indegree.items() if deg == 0]
    visited = 0
    indegree_copy = dict(indegree)
    while queue:
        n = queue.pop()
        visited += 1
        for nxt in forward.get(n, []):
            indegree_copy[nxt] -= 1
            if indegree_copy[nxt] == 0:
                queue.append(nxt)
    return visited != len(DEPARTMENT_DEPENDENCIES)


def full_lifecycle_report(completed_departments: Set[str]) -> Dict[str, DependencyCheckResult]:
    """One DependencyCheckResult per pipeline department, given a set of
    departments assumed already completed for one project."""
    return {d: validate_dependency(d, completed_departments) for d in ALL_PIPELINE_DEPARTMENTS}
