"""
services/incident_response/routing.py — LangGraph conditional-edge predicates.
=======================================================================
Kept separate from workflows/incident_response_graph.py so routing logic
can be unit-tested without constructing a real StateGraph. Mirrors
services/monitoring/routing.py, services/devops/routing.py.
"""
from __future__ import annotations

from typing import Any, Dict

MAX_TASK_RETRIES = 3


def route_after_intake(state: Dict[str, Any]) -> str:
    """Every incident is analyzed, no exceptions."""
    return "analyze"


def route_after_analyze(state: Dict[str, Any]) -> str:
    """
    Skip the recovery stage entirely when the classifier recommended no
    action (spec §4: Incident Response reacts to what's needed, it
    doesn't force a rollback/restart on every incident).
    """
    if state.get("recommended_action") in (None, "none"):
        return "communicate"
    return "recover"


def route_after_recover(state: Dict[str, Any]) -> str:
    return "communicate"


def route_after_communicate(state: Dict[str, Any]) -> str:
    return "finalize"


def route_task_retry(task_state: Dict[str, Any]) -> str:
    if task_state.get("status") == "completed":
        return "done"
    retries = task_state.get("retry_count", 0)
    if retries < MAX_TASK_RETRIES:
        return "retry"
    return "dead_letter"
