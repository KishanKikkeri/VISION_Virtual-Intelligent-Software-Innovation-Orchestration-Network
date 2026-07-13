"""
services/monitoring/routing.py — LangGraph conditional-edge predicates.
=======================================================================
Kept separate from workflows/monitoring_graph.py so routing logic can
be unit-tested without constructing a real StateGraph. Mirrors
services/devops/routing.py, services/qa/routing, services/security/routing.
"""
from __future__ import annotations

from typing import Any, Dict

MAX_TASK_RETRIES = 3


def route_after_collect(state: Dict[str, Any]) -> str:
    """
    Even a fully-degraded collection (every provider returned 0) still
    routes forward to score_and_publish — a CRITICAL health score IS
    the correct signal in that case, not a reason to abort the cycle
    (spec §7/§8: providers degrade, they never abort the cycle).
    """
    return "score_and_publish"


def route_after_score_and_publish(state: Dict[str, Any]) -> str:
    """Dashboard/Alert always run every cycle (spec §3 step 5)."""
    return "dashboard_and_alert"


def route_after_dashboard_and_alert(state: Dict[str, Any]) -> str:
    """
    incident_candidates is populated by score_and_publish (MonitoringHead
    already ran decide_incident before this node) — this predicate just
    decides whether the graph's optional incident-handoff publish step runs.
    """
    if state.get("incident_candidates"):
        return "incident_handoff"
    return "publish"


def route_after_incident_handoff(state: Dict[str, Any]) -> str:
    return "publish"


def route_task_retry(task_state: Dict[str, Any]) -> str:
    if task_state.get("status") == "completed":
        return "done"
    retries = task_state.get("retry_count", 0)
    if retries < MAX_TASK_RETRIES:
        return "retry"
    return "dead_letter"
