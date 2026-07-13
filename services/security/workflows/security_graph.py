"""
services/security/workflows/security_graph.py
==================================================
W-SEC: Security Service LangGraph state machine.

Stages (per M3.5 spec's LangGraph Workflow section):
  receive_engineering  -> validate_inputs
  validate_inputs      -> static_analysis
  static_analysis      -> parallel_fan_out (Secret / Dependency / Compliance
                          / License / SBOM Generation)
  parallel_fan_out     -> risk_classification
  risk_classification  -> aggregate_results
  aggregate_results    -> PASS/WARN: publish | FAIL: security_findings -> return_to_engineering

Supports: retry, escalation, DLQ, Postgres checkpoints — mirroring
services/qa/workflows/qa_graph.py.

The SecurityHead agent (services/security/head) handles the actual
business logic when this graph is driven from the real agent pipeline.
This graph is the durable, resumable state machine used by the
platform runtime.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog
from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from services.security.routing import (
    MAX_RETRY_CYCLES,
    route_after_aggregate,
    route_after_fan_out,
    route_after_risk_classification,
    route_after_security_findings,
    route_after_static_analysis,
    route_after_validate_inputs,
)

log = structlog.get_logger(__name__)


class SecurityState(TypedDict):
    project_id:          str
    workflow_id:         str
    feature_name:        str

    inputs_valid:         bool
    static_analysis_ready: bool
    secret_scan_ready:    bool
    dependency_scan_ready: bool
    compliance_ready:     bool

    risk_score:           float
    verdict:              str          # pass | warn | fail
    retry_cycles_run:     int

    any_dead_lettered:    bool
    dlq_tasks:            List[str]

    phase_status:         str
    failure_reason:       Optional[str]
    resume_at_stage:      Optional[str]

    nats_events_queue:    List[Dict[str, Any]]
    ws_events_queue:      List[Dict[str, Any]]


async def receive_engineering_node(state: SecurityState) -> Dict[str, Any]:
    log.info("security_graph_receive", project_id=state["project_id"])
    return {
        "phase_status": "running",
        "ws_events_queue": [{
            "project_id": state["project_id"], "event_type": "phase_started",
            "payload": {"phase": 6, "phase_name": "Security Validation"},
        }],
    }


async def validate_inputs_node(state: SecurityState) -> Dict[str, Any]:
    valid = bool(state.get("inputs_valid", True))
    log.info("security_graph_validate", project_id=state["project_id"], valid=valid)
    return {
        "phase_status": "running" if valid else "failed",
        "failure_reason": None if valid else "Missing required Engineering artifacts",
        "nats_events_queue": [{
            "subject": "security.phase.started",
            "payload": {"project_id": state["project_id"], "feature_name": state.get("feature_name", "default")},
        }],
    }


async def static_analysis_node(state: SecurityState) -> Dict[str, Any]:
    log.info("security_graph_static_analysis", project_id=state["project_id"])
    return {"static_analysis_ready": True}


async def fan_out_node(state: SecurityState) -> Dict[str, Any]:
    log.info("security_graph_fan_out", project_id=state["project_id"])
    return {}


async def secret_scan_node(state: SecurityState) -> Dict[str, Any]:
    return {"secret_scan_ready": True}


async def dependency_scan_node(state: SecurityState) -> Dict[str, Any]:
    return {"dependency_scan_ready": True}


async def compliance_scan_node(state: SecurityState) -> Dict[str, Any]:
    return {"compliance_ready": True}


async def risk_classification_node(state: SecurityState) -> Dict[str, Any]:
    log.info("security_graph_risk", project_id=state["project_id"], risk_score=state.get("risk_score", 0.0))
    return {
        "nats_events_queue": [{
            "subject": "security.scan.completed",
            "payload": {"project_id": state["project_id"], "risk_score": state.get("risk_score", 0.0)},
        }],
    }


async def aggregate_results_node(state: SecurityState) -> Dict[str, Any]:
    all_ready = all([
        state.get("static_analysis_ready"), state.get("secret_scan_ready"),
        state.get("dependency_scan_ready"), state.get("compliance_ready"),
    ])
    log.info("security_graph_aggregate", project_id=state["project_id"], all_ready=all_ready)
    return {
        "phase_status": "running" if all_ready else "failed",
        "failure_reason": None if all_ready else "One or more Security teams failed",
    }


async def security_findings_node(state: SecurityState) -> Dict[str, Any]:
    cycles = state.get("retry_cycles_run", 0) + 1
    log.info("security_graph_findings", project_id=state["project_id"], cycle=cycles)
    return {
        "retry_cycles_run": cycles,
        "nats_events_queue": [{
            "subject": "security.findings.created",
            "payload": {"project_id": state["project_id"], "cycle": cycles},
        }],
    }


async def return_to_engineering_node(state: SecurityState) -> Dict[str, Any]:
    log.info("security_graph_retry_requested", project_id=state["project_id"])
    return {
        "phase_status": "failed",
        "nats_events_queue": [{
            "subject": "security.retry.requested",
            "payload": {"project_id": state["project_id"], "reason": state.get("failure_reason", "")},
        }],
        "ws_events_queue": [{
            "project_id": state["project_id"], "event_type": "phase_failed",
            "payload": {"phase": 6, "reason": state.get("failure_reason", "")},
        }],
    }


async def dlq_node(state: SecurityState) -> Dict[str, Any]:
    log.error("security_graph_dlq", project_id=state["project_id"], tasks=state.get("dlq_tasks", []))
    return {
        "phase_status": "failed",
        "failure_reason": f"Dead-lettered tasks: {state.get('dlq_tasks', [])}",
        "nats_events_queue": [{
            "subject": "security.phase.failed",
            "payload": {"project_id": state["project_id"], "tasks": state.get("dlq_tasks", [])},
        }],
    }


async def publish_artifacts_node(state: SecurityState) -> Dict[str, Any]:
    log.info("security_graph_publish", project_id=state["project_id"])
    return {
        "phase_status": "completed",
        "nats_events_queue": [{
            "subject": "security.phase.completed",
            "payload": {
                "project_id": state["project_id"], "workflow_id": state.get("workflow_id"),
                "feature_name": state.get("feature_name"), "risk_score": state.get("risk_score", 0.0),
            },
        }],
        "ws_events_queue": [{
            "project_id": state["project_id"], "event_type": "phase_completed",
            "payload": {"phase": 6, "phase_name": "Security Validation",
                        "message": "Security passed — ready for Manager/DevOps"},
        }],
    }


async def handle_failure_node(state: SecurityState) -> Dict[str, Any]:
    log.error("security_graph_failed", project_id=state["project_id"], reason=state.get("failure_reason", ""))
    return {
        "phase_status": "failed",
        "nats_events_queue": [{
            "subject": "security.phase.failed",
            "payload": {"project_id": state["project_id"], "reason": state.get("failure_reason", "")},
        }],
        "ws_events_queue": [{
            "project_id": state["project_id"], "event_type": "phase_failed",
            "payload": {"phase": 6, "reason": state.get("failure_reason", "")},
        }],
    }


def build_security_graph(checkpointer=None):
    """Builds W-SEC — Security Service LangGraph."""
    g = StateGraph(SecurityState)

    g.add_node("receive_engineering",   receive_engineering_node)
    g.add_node("validate_inputs",       validate_inputs_node)
    g.add_node("static_analysis",       static_analysis_node)
    g.add_node("fan_out",               fan_out_node)
    g.add_node("secret_scan",           secret_scan_node)
    g.add_node("dependency_scan",       dependency_scan_node)
    g.add_node("compliance_scan",       compliance_scan_node)
    g.add_node("risk_classification",   risk_classification_node)
    g.add_node("aggregate_results",     aggregate_results_node)
    g.add_node("security_findings",     security_findings_node)
    g.add_node("return_to_engineering", return_to_engineering_node)
    g.add_node("dlq",                   dlq_node)
    g.add_node("publish_artifacts",     publish_artifacts_node)
    g.add_node("handle_failure",        handle_failure_node)

    g.set_entry_point("receive_engineering")
    g.add_edge("receive_engineering", "validate_inputs")

    g.add_conditional_edges("validate_inputs", route_after_validate_inputs, {
        "static_analysis": "static_analysis",
        "failed":          "handle_failure",
    })

    g.add_conditional_edges("static_analysis", route_after_static_analysis, {
        "fan_out": "fan_out",
        "dlq":     "dlq",
        "failed":  "handle_failure",
    })

    g.add_edge("fan_out", "secret_scan")
    g.add_edge("fan_out", "dependency_scan")
    g.add_edge("fan_out", "compliance_scan")

    g.add_edge("secret_scan",     "risk_classification")
    g.add_edge("dependency_scan", "risk_classification")
    g.add_edge("compliance_scan", "risk_classification")

    g.add_conditional_edges("risk_classification", route_after_risk_classification, {
        "aggregate": "aggregate_results",
    })

    g.add_conditional_edges("aggregate_results", route_after_aggregate, {
        "publish":          "publish_artifacts",
        "security_findings": "security_findings",
    })
    g.add_conditional_edges("security_findings", route_after_security_findings, {
        "return_to_engineering": "return_to_engineering",
        "failed":                "handle_failure",
    })

    g.add_edge("dlq",                   "handle_failure")
    g.add_edge("return_to_engineering", "handle_failure")
    g.add_edge("publish_artifacts",     END)
    g.add_edge("handle_failure",        END)

    kwargs: Dict[str, Any] = {}
    if checkpointer:
        kwargs["checkpointer"] = checkpointer

    return g.compile(**kwargs)
