"""
services/engineering/routing — LangGraph conditional-edge predicates.
=========================================================================
Kept separate from workflows/engineering_graph.py so routing logic can
be unit-tested without constructing a real StateGraph.
"""
from __future__ import annotations

from typing import Any, Dict

MAX_REVIEW_CYCLES   = 3
MAX_TASK_RETRIES     = 3
MAX_TRACEABILITY_RERUNS = 2


def route_after_plan(state: Dict[str, Any]) -> str:
    if state.get("phase_status") == "failed":
        return "failed"
    return "fan_out"


def route_after_fan_out(state: Dict[str, Any]) -> str:
    if state.get("phase_status") == "failed":
        return "failed"
    if state.get("any_dead_lettered"):
        return "dlq"
    return "aggregate"


def route_after_aggregate(state: Dict[str, Any]) -> str:
    if not state.get("all_tasks_complete", False):
        return "fan_out"          # more dependency-scheduled batches remain
    return "review"


def route_after_review(state: Dict[str, Any]) -> str:
    verdict = state.get("review_verdict", "block")
    cycles  = state.get("review_cycles_run", 0)
    if verdict == "pass":
        return "repository"
    if verdict == "revise" and cycles < MAX_REVIEW_CYCLES:
        return "revise"
    return "failed"


def route_after_repository(state: Dict[str, Any]) -> str:
    if state.get("phase_status") == "failed":
        return "failed"
    return "publish"


def route_task_retry(task_state: Dict[str, Any]) -> str:
    """Per-task retry routing used inside the review/repository escalation loop."""
    if task_state.get("status") == "completed":
        return "done"
    retries = task_state.get("retry_count", 0)
    if retries < MAX_TASK_RETRIES:
        return "retry"
    return "dead_letter"


def route_checkpoint_recovery(state: Dict[str, Any]) -> str:
    """
    Used on graph resume: if a checkpoint shows an in-flight stage, resume
    at that stage rather than restarting Stage 1.
    """
    stage = state.get("resume_at_stage")
    valid = {"plan", "fan_out", "aggregate", "review", "repository", "publish"}
    if stage in valid:
        return stage
    return "plan"
