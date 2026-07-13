"""
services/devops/routing.py — LangGraph conditional-edge predicates.
=======================================================================
Kept separate from workflows/devops_graph.py so routing logic can be
unit-tested without constructing a real StateGraph. Mirrors
services/qa/routing and services/security/routing.
"""
from __future__ import annotations

from typing import Any, Dict

MAX_RETRY_CYCLES = 3
MAX_TASK_RETRIES = 3


def route_after_validate_qa_security(state: Dict[str, Any]) -> str:
    if state.get("phase_status") == "failed":
        return "failed"
    return "generate_infrastructure"


def route_after_generate_infrastructure(state: Dict[str, Any]) -> str:
    if state.get("phase_status") == "failed":
        return "failed"
    if state.get("any_dead_lettered"):
        return "dlq"
    return "generate_cicd"


def route_after_generate_cicd(state: Dict[str, Any]) -> str:
    if state.get("phase_status") == "failed":
        return "failed"
    return "approval_gate"


def route_after_approval_gate(state: Dict[str, Any]) -> str:
    """
    This graph's OWN approval interrupt, used when DevOps runs
    standalone (see workflows/devops_graph.py docstring). In the
    platform's normal in-process runtime, the actual interrupt is
    Manager's `deployment_approval_gate_node`
    (services/manager/graphs/lifecycle.py) — this predicate exists so
    the same durable state machine works identically if DevOps is ever
    driven independently over NATS.
    """
    status = state.get("approval_status")
    if status == "approved":
        return "deploy"
    if status == "rejected":
        return "failed"
    return "awaiting_approval"


def route_after_deploy(state: Dict[str, Any]) -> str:
    if state.get("phase_status") == "failed":
        return "failed"
    return "health_check"


def route_after_health_check(state: Dict[str, Any]) -> str:
    if state.get("health_passed"):
        return "release"
    return "rollback"


def route_after_rollback(state: Dict[str, Any]) -> str:
    cycles = state.get("retry_cycles_run", 0)
    if cycles < MAX_RETRY_CYCLES:
        return "notify_manager_failed"
    return "failed"


def route_task_retry(task_state: Dict[str, Any]) -> str:
    if task_state.get("status") == "completed":
        return "done"
    retries = task_state.get("retry_count", 0)
    if retries < MAX_TASK_RETRIES:
        return "retry"
    return "dead_letter"


def route_checkpoint_recovery(state: Dict[str, Any]) -> str:
    stage = state.get("resume_at_stage")
    valid = {"validate", "generate_infrastructure", "generate_cicd", "approval_gate",
             "deploy", "health_check", "rollback", "release"}
    if stage in valid:
        return stage
    return "validate"
