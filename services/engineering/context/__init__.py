"""
services/engineering/context — task decomposition & dependency scheduling.
=============================================================================
Turns approved Architecture artifacts into an ImplementationPlan
(Stage 1: "Implementation Plan" -> "Task Breakdown" in the graph),
and provides dependency-aware batch scheduling used by Stage 3
("Parallel Fan-Out").
"""
from __future__ import annotations

from typing import Any, Dict, List, Set

from services.engineering.models import (
    EngineeringTask,
    EngineeringTeam,
    ImplementationPlan,
)

# Worker assignment per team, in the fixed order the spec defines.
BACKEND_WORKERS = [
    "database_layer_worker",
    "authentication_worker",
    "business_logic_worker",
    "api_implementation_worker",
]
FRONTEND_WORKERS = [
    "component_worker",
    "page_worker",
    "state_management_worker",
    "routing_worker",
]
INTEGRATION_WORKERS = [
    "internal_integration_worker",
    "third_party_integration_worker",
    "messaging_worker",
]

# Intra-team dependency edges (worker_id -> [worker_ids it depends on]).
# These encode realistic build order without inventing cross-team coupling —
# cross-team ordering is enforced by the graph's fan-out/aggregate structure.
_BACKEND_DEPS = {
    "database_layer_worker":     [],
    "authentication_worker":     ["database_layer_worker"],
    "business_logic_worker":     ["database_layer_worker"],
    "api_implementation_worker": ["authentication_worker", "business_logic_worker"],
}
_FRONTEND_DEPS = {
    "state_management_worker": [],
    "component_worker":        ["state_management_worker"],
    "routing_worker":          ["state_management_worker"],
    "page_worker":              ["component_worker", "routing_worker"],
}
_INTEGRATION_DEPS = {
    "internal_integration_worker":     [],
    "third_party_integration_worker":  [],
    "messaging_worker":                ["internal_integration_worker"],
}


def build_implementation_plan(
    project_id: str,
    feature_name: str,
    architecture_refs: Dict[str, Any],
    include_frontend: bool = True,
) -> ImplementationPlan:
    """
    Stage 1 → Stage 2: Implementation Plan + Task Breakdown.
    Backend tasks are always included. Frontend tasks are included only
    when a ui_blueprint is present in architecture_refs (per Stage 3 rule:
    "These must consume ui_blueprint. No UI generation without it.").
    Integration tasks are always included (NATS/external contracts exist
    regardless of UI).
    """
    plan = ImplementationPlan(
        project_id=project_id,
        feature_name=feature_name,
        architecture_refs=architecture_refs,
    )

    id_by_worker: Dict[str, str] = {}

    def _add(team: EngineeringTeam, worker_id: str, deps_worker_ids: List[str]) -> None:
        task = EngineeringTask(
            project_id=project_id,
            team=team,
            worker_agent_id=worker_id,
            description=f"{team.value}:{worker_id} for feature '{feature_name}'",
            depends_on=[id_by_worker[d] for d in deps_worker_ids if d in id_by_worker],
        )
        id_by_worker[worker_id] = task.task_id
        plan.tasks.append(task)

    for w in BACKEND_WORKERS:
        _add(EngineeringTeam.BACKEND, w, _BACKEND_DEPS.get(w, []))

    has_ui_blueprint = bool(architecture_refs.get("ui_blueprint"))
    if include_frontend and has_ui_blueprint:
        for w in FRONTEND_WORKERS:
            _add(EngineeringTeam.FRONTEND, w, _FRONTEND_DEPS.get(w, []))

    for w in INTEGRATION_WORKERS:
        _add(EngineeringTeam.INTEGRATION, w, _INTEGRATION_DEPS.get(w, []))

    return plan


def topological_batches(tasks: List[EngineeringTask]) -> List[List[EngineeringTask]]:
    """
    Splits tasks into ordered batches such that every task in batch N only
    depends on tasks in batches < N. Used for dependency-aware scheduling
    within a single team's parallel fan-out.
    Raises ValueError on a dependency cycle.
    """
    remaining = {t.task_id: t for t in tasks}
    done: Set[str] = set()
    batches: List[List[EngineeringTask]] = []

    while remaining:
        batch = [t for t in remaining.values() if all(d in done for d in t.depends_on)]
        if not batch:
            raise ValueError(
                f"Dependency cycle detected among tasks: {list(remaining.keys())}"
            )
        batches.append(batch)
        for t in batch:
            done.add(t.task_id)
            del remaining[t.task_id]

    return batches


def team_progress(plan: ImplementationPlan, team: EngineeringTeam) -> Dict[str, int]:
    team_tasks = plan.tasks_by_team(team)
    return {
        "total":     len(team_tasks),
        "completed": sum(1 for t in team_tasks if t.status.value == "completed"),
        "failed":    sum(1 for t in team_tasks if t.status.value == "failed"),
        "escalated": sum(1 for t in team_tasks if t.escalated),
    }
