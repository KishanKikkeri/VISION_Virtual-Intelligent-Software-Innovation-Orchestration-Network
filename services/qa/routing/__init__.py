"""
services/qa/routing — LangGraph conditional-edge predicates.
=================================================================
Kept separate from workflows/qa_graph.py so routing logic can be
unit-tested without constructing a real StateGraph. Mirrors
services/engineering/routing.
"""
from __future__ import annotations

from typing import Any, Dict

MAX_RETRY_CYCLES     = 3
MAX_TASK_RETRIES     = 3


def route_after_validate_inputs(state: Dict[str, Any]) -> str:
    if state.get("phase_status") == "failed":
        return "failed"
    return "generate"


def route_after_generate(state: Dict[str, Any]) -> str:
    if state.get("phase_status") == "failed":
        return "failed"
    if state.get("any_dead_lettered"):
        return "dlq"
    return "execute"


def route_after_execute(state: Dict[str, Any]) -> str:
    if state.get("phase_status") == "failed":
        return "failed"
    return "coverage"


def route_after_coverage(state: Dict[str, Any]) -> str:
    return "aggregate"


def route_after_aggregate(state: Dict[str, Any]) -> str:
    verdict = state.get("verdict", "fail")
    if verdict == "fail":
        return "defect_report"
    return "publish"


def route_after_defect_report(state: Dict[str, Any]) -> str:
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
    valid = {"validate", "generate", "execute", "coverage", "aggregate", "publish"}
    if stage in valid:
        return stage
    return "validate"
