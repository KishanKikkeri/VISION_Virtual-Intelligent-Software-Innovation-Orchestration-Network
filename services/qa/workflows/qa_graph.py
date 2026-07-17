"""
services/qa/workflows/qa_graph.py
====================================
W-QA: QA Service LangGraph state machine.

Stages (per M3.4 spec's LangGraph Workflow section):
  receive_artifacts    -> validate_inputs
  validate_inputs      -> generate_test_suites (Unit + Integration)
  generate_test_suites -> parallel_test_execution (Regression + Performance)
  parallel_test_execution -> coverage_analysis
  coverage_analysis    -> aggregate_results
  aggregate_results    -> PASS: publish | FAIL: defect_report -> return_to_engineering

Supports: retries, escalation, checkpoint recovery, DLQ routing —
mirroring services/engineering/workflows/engineering_graph.py.

The QAHead agent (services/qa/head) handles the actual business logic
when this graph is driven from the real agent pipeline. This graph is
the durable, resumable state machine used by the platform runtime.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog
from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from services.qa.routing import (
    MAX_RETRY_CYCLES,
    route_after_aggregate,
    route_after_coverage,
    route_after_defect_report,
    route_after_execute,
    route_after_generate,
    route_after_validate_inputs,
)

log = structlog.get_logger(__name__)


class QAState(TypedDict):
    project_id:         str
    workflow_id:        str
    feature_name:       str

    inputs_valid:        bool
    unit_ready:          bool
    integration_ready:   bool
    regression_ready:    bool
    performance_ready:   bool

    coverage_pct:        float
    verdict:             str          # pass | warn | fail
    retry_cycles_run:    int

    any_dead_lettered:   bool
    dlq_tasks:           List[str]

    phase_status:        str
    failure_reason:      Optional[str]
    resume_at_stage:     Optional[str]

    nats_events_queue:   List[Dict[str, Any]]
    ws_events_queue:     List[Dict[str, Any]]


async def receive_artifacts_node(state: QAState) -> Dict[str, Any]:
    log.info("qa_graph_receive", project_id=state["project_id"])
    return {
        "phase_status": "running",
        "ws_events_queue": [{
            "project_id": state["project_id"], "event_type": "phase_started",
            "payload": {"phase": 5, "phase_name": "QA Validation"},
        }],
    }


async def validate_inputs_node(state: QAState) -> Dict[str, Any]:
    valid = bool(state.get("inputs_valid", True))
    log.info("qa_graph_validate", project_id=state["project_id"], valid=valid)
    return {
        "phase_status": "running" if valid else "failed",
        "failure_reason": None if valid else "Missing required Engineering artifacts",
        "nats_events_queue": [{
            "subject": "qa.phase.started",
            "payload": {"project_id": state["project_id"], "feature_name": state.get("feature_name", "default")},
        }],
    }


async def fan_out_generate_node(state: QAState) -> Dict[str, Any]:
    log.info("qa_graph_fan_out_generate", project_id=state["project_id"])
    return {}


async def generate_unit_node(state: QAState) -> Dict[str, Any]:
    return {"unit_ready": True}


async def generate_integration_node(state: QAState) -> Dict[str, Any]:
    return {"integration_ready": True}


async def generate_complete_node(state: QAState) -> Dict[str, Any]:
    """Join point for the Unit/Integration test-generation fan-out —
    mirrors services/engineering/workflows/engineering_graph.py's
    aggregate_results join. Gives `route_after_generate` a place to
    check `any_dead_lettered` *before* fanning out to the execute
    stage, so tasks that exhausted their retries (see
    services/qa/routing.route_task_retry's "dead_letter" outcome) are
    routed to the `dlq` node instead of silently proceeding."""
    log.info("qa_graph_generate_complete", project_id=state["project_id"],
             any_dead_lettered=state.get("any_dead_lettered", False))
    return {}


async def fan_out_execute_node(state: QAState) -> Dict[str, Any]:
    log.info("qa_graph_fan_out_execute", project_id=state["project_id"])
    return {}


async def execute_regression_node(state: QAState) -> Dict[str, Any]:
    return {"regression_ready": True}


async def execute_performance_node(state: QAState) -> Dict[str, Any]:
    return {"performance_ready": True}


async def coverage_analysis_node(state: QAState) -> Dict[str, Any]:
    log.info("qa_graph_coverage", project_id=state["project_id"], coverage=state.get("coverage_pct", 0.0))
    return {
        "nats_events_queue": [{
            "subject": "qa.coverage.completed",
            "payload": {"project_id": state["project_id"], "coverage_pct": state.get("coverage_pct", 0.0)},
        }],
    }


async def aggregate_results_node(state: QAState) -> Dict[str, Any]:
    all_ready = all([
        state.get("unit_ready"), state.get("integration_ready"),
        state.get("regression_ready"), state.get("performance_ready"),
    ])
    log.info("qa_graph_aggregate", project_id=state["project_id"], all_ready=all_ready)
    return {
        "phase_status": "running" if all_ready else "failed",
        "failure_reason": None if all_ready else "One or more QA teams failed",
    }


async def defect_report_node(state: QAState) -> Dict[str, Any]:
    cycles = state.get("retry_cycles_run", 0) + 1
    log.info("qa_graph_defect_report", project_id=state["project_id"], cycle=cycles)
    return {
        "retry_cycles_run": cycles,
        "nats_events_queue": [{
            "subject": "qa.defect.created",
            "payload": {"project_id": state["project_id"], "cycle": cycles},
        }],
    }


async def return_to_engineering_node(state: QAState) -> Dict[str, Any]:
    log.info("qa_graph_retry_requested", project_id=state["project_id"])
    return {
        "phase_status": "failed",
        "nats_events_queue": [{
            "subject": "qa.retry.requested",
            "payload": {"project_id": state["project_id"], "reason": state.get("failure_reason", "")},
        }],
        "ws_events_queue": [{
            "project_id": state["project_id"], "event_type": "phase_failed",
            "payload": {"phase": 5, "reason": state.get("failure_reason", "")},
        }],
    }


async def dlq_node(state: QAState) -> Dict[str, Any]:
    log.error("qa_graph_dlq", project_id=state["project_id"], tasks=state.get("dlq_tasks", []))
    return {
        "phase_status": "failed",
        "failure_reason": f"Dead-lettered tasks: {state.get('dlq_tasks', [])}",
        "nats_events_queue": [{
            "subject": "qa.phase.failed",
            "payload": {"project_id": state["project_id"], "tasks": state.get("dlq_tasks", [])},
        }],
    }


async def publish_artifacts_node(state: QAState) -> Dict[str, Any]:
    log.info("qa_graph_publish", project_id=state["project_id"])
    return {
        "phase_status": "completed",
        "nats_events_queue": [{
            "subject": "qa.phase.completed",
            "payload": {
                "project_id": state["project_id"], "workflow_id": state.get("workflow_id"),
                "feature_name": state.get("feature_name"), "coverage_pct": state.get("coverage_pct", 0.0),
            },
        }],
        "ws_events_queue": [{
            "project_id": state["project_id"], "event_type": "phase_completed",
            "payload": {"phase": 5, "phase_name": "QA Validation",
                        "message": "QA passed — ready for Security/DevOps"},
        }],
    }


async def handle_failure_node(state: QAState) -> Dict[str, Any]:
    log.error("qa_graph_failed", project_id=state["project_id"], reason=state.get("failure_reason", ""))
    return {
        "phase_status": "failed",
        "nats_events_queue": [{
            "subject": "qa.phase.failed",
            "payload": {"project_id": state["project_id"], "reason": state.get("failure_reason", "")},
        }],
        "ws_events_queue": [{
            "project_id": state["project_id"], "event_type": "phase_failed",
            "payload": {"phase": 5, "reason": state.get("failure_reason", "")},
        }],
    }


def build_qa_graph(checkpointer=None):
    """Builds W-QA — QA Service LangGraph."""
    g = StateGraph(QAState)

    g.add_node("receive_artifacts",      receive_artifacts_node)
    g.add_node("validate_inputs",        validate_inputs_node)
    g.add_node("fan_out_generate",       fan_out_generate_node)
    g.add_node("generate_unit",          generate_unit_node)
    g.add_node("generate_integration",   generate_integration_node)
    g.add_node("generate_complete",      generate_complete_node)
    g.add_node("fan_out_execute",        fan_out_execute_node)
    g.add_node("execute_regression",     execute_regression_node)
    g.add_node("execute_performance",    execute_performance_node)
    g.add_node("coverage_analysis",      coverage_analysis_node)
    g.add_node("aggregate_results",      aggregate_results_node)
    g.add_node("defect_report",          defect_report_node)
    g.add_node("return_to_engineering",  return_to_engineering_node)
    g.add_node("dlq",                    dlq_node)
    g.add_node("publish_artifacts",      publish_artifacts_node)
    g.add_node("handle_failure",         handle_failure_node)

    g.set_entry_point("receive_artifacts")
    g.add_edge("receive_artifacts", "validate_inputs")

    g.add_conditional_edges("validate_inputs", route_after_validate_inputs, {
        "generate": "fan_out_generate",
        "failed":   "handle_failure",
    })

    g.add_edge("fan_out_generate", "generate_unit")
    g.add_edge("fan_out_generate", "generate_integration")
    g.add_edge("generate_unit",        "generate_complete")
    g.add_edge("generate_integration", "generate_complete")

    g.add_conditional_edges("generate_complete", route_after_generate, {
        "execute": "fan_out_execute",
        "dlq":     "dlq",
        "failed":  "handle_failure",
    })
    g.add_edge("fan_out_execute", "execute_regression")
    g.add_edge("fan_out_execute", "execute_performance")

    g.add_edge("execute_regression",  "coverage_analysis")
    g.add_edge("execute_performance", "coverage_analysis")
    g.add_edge("coverage_analysis",   "aggregate_results")

    g.add_conditional_edges("aggregate_results", route_after_aggregate, {
        "publish": "publish_artifacts",
        "fail":    "defect_report",
    })
    g.add_conditional_edges("defect_report", route_after_defect_report, {
        "return_to_engineering": "return_to_engineering",
        "failed":                "handle_failure",
    })

    g.add_edge("dlq",                  "handle_failure")
    g.add_edge("return_to_engineering", "handle_failure")
    g.add_edge("publish_artifacts",     END)
    g.add_edge("handle_failure",        END)

    kwargs: Dict[str, Any] = {}
    if checkpointer:
        kwargs["checkpointer"] = checkpointer

    return g.compile(**kwargs)
