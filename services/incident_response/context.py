"""
services/incident_response/context.py — task decomposition for M3.8.
================================================================
Design decision (mirrors docs/M3.7_Monitoring_Service_Specification_v1.md
§1/§2's precedent): AGENT_REGISTRY reserves exactly 10 agent_ids for the
`incident_response` department — 1 head, 3 leads (analysis/recovery/
communication), 6 workers. Every step here maps 1:1 to a real agent
invocation, invoked from services/incident_response/workflows/
incident_response_graph.py exactly like services/monitoring/workflows/
monitoring_graph.py's nodes call factory.create(agent_id).run(task).
"""
from __future__ import annotations

import asyncio
from typing import Dict, List, Optional, Set

from services.incident_response.models import IncidentTask, IncidentTaskStatus

# Worker assignment per team, matching AGENT_REGISTRY's incident_response entries.
ANALYSIS_WORKERS      = ["incident_classifier_worker", "evidence_collection_worker"]
RECOVERY_WORKERS      = ["rollback_worker", "recovery_worker"]
COMMUNICATION_WORKERS = ["notification_worker", "reporting_worker"]

# evidence_collection_worker runs after incident_classifier_worker so
# evidence gathering can be scoped by the classifier's recommended
# action (e.g. only fetch deployment history when rollback is in play).
_ANALYSIS_DEPS: Dict[str, List[str]] = {
    "incident_classifier_worker": [],
    "evidence_collection_worker": ["incident_classifier_worker"],
}
# rollback_worker must complete (or be skipped) before recovery_worker
# verifies the outcome — they are NOT independent, unlike Monitoring's
# alert/dashboard pair.
_RECOVERY_DEPS: Dict[str, List[str]] = {
    "rollback_worker": [],
    "recovery_worker": ["rollback_worker"],
}
_COMMUNICATION_DEPS: Dict[str, List[str]] = {
    "notification_worker": [],
    "reporting_worker": [],
}


def build_incident_tasks() -> List[IncidentTask]:
    """One incident's full task list — Analyze -> Recover -> Communicate."""
    tasks: List[IncidentTask] = []
    id_by_worker: Dict[str, str] = {}

    def _add(worker_id: str, deps_worker_ids: List[str]) -> None:
        task = IncidentTask(
            worker_agent_id=worker_id,
            description=f"incident lifecycle step: {worker_id}",
            depends_on=[id_by_worker[d] for d in deps_worker_ids if d in id_by_worker],
        )
        id_by_worker[worker_id] = task.task_id
        tasks.append(task)

    for w in ANALYSIS_WORKERS:
        _add(w, _ANALYSIS_DEPS.get(w, []))
    for w in RECOVERY_WORKERS:
        _add(w, _RECOVERY_DEPS.get(w, []))
    for w in COMMUNICATION_WORKERS:
        _add(w, _COMMUNICATION_DEPS.get(w, []))

    return tasks


def topological_batches(tasks: List[IncidentTask]) -> List[List[IncidentTask]]:
    remaining = {t.task_id: t for t in tasks}
    done: Set[str] = set()
    batches: List[List[IncidentTask]] = []

    while remaining:
        batch = [t for t in remaining.values() if all(d in done for d in t.depends_on)]
        if not batch:
            raise ValueError(f"Dependency cycle detected among Incident Response tasks: {list(remaining.keys())}")
        batches.append(batch)
        for t in batch:
            done.add(t.task_id)
            del remaining[t.task_id]

    return batches


def team_progress(tasks: List[IncidentTask]) -> Dict[str, int]:
    return {
        "total":     len(tasks),
        "completed": sum(1 for t in tasks if t.status == IncidentTaskStatus.COMPLETED),
        "failed":    sum(1 for t in tasks if t.status == IncidentTaskStatus.FAILED),
        "escalated": sum(1 for t in tasks if t.status == IncidentTaskStatus.ESCALATED),
    }
