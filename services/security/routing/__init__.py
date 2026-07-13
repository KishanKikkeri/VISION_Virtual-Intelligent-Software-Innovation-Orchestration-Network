"""
services/security/routing — LangGraph conditional-edge predicates.
=====================================================================
Kept separate from workflows/security_graph.py so routing logic can be
unit-tested without constructing a real StateGraph. Mirrors
services/qa/routing.
"""
from __future__ import annotations

from typing import Any, Dict

MAX_RETRY_CYCLES = 3
MAX_TASK_RETRIES = 3


def route_after_validate_inputs(state: Dict[str, Any]) -> str:
    if state.get("phase_status") == "failed":
        return "failed"
    return "static_analysis"


def route_after_static_analysis(state: Dict[str, Any]) -> str:
    if state.get("phase_status") == "failed":
        return "failed"
    if state.get("any_dead_lettered"):
        return "dlq"
    return "fan_out"


def route_after_fan_out(state: Dict[str, Any]) -> str:
    if state.get("phase_status") == "failed":
        return "failed"
    return "risk_classification"


def route_after_risk_classification(state: Dict[str, Any]) -> str:
    return "aggregate"


def route_after_aggregate(state: Dict[str, Any]) -> str:
    verdict = state.get("verdict", "fail")
    if verdict == "fail":
        return "security_findings"
    return "publish"


def route_after_security_findings(state: Dict[str, Any]) -> str:
    cycles = state.get("retry_cycles_run", 0)
    if cycles < MAX_RETRY_CYCLES:
        return "return_to_engineering"
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
    valid = {"validate", "static_analysis", "fan_out", "risk_classification", "aggregate", "publish"}
    if stage in valid:
        return stage
    return "validate"
