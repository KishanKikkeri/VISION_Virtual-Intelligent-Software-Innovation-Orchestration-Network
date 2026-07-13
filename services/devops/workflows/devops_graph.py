"""
services/devops/workflows/devops_graph.py
=============================================
W-DEVOPS: DevOps Service LangGraph state machine.

NOTE on the approval interrupt: in the platform's normal in-process
runtime, the REAL deployment approval interrupt is Manager's own
`deployment_approval_gate_node` (services/manager/graphs/lifecycle.py) —
Manager pauses ITS graph, a human approves via `POST
/projects/{id}/approve`, and Manager then invokes DevOpsHead a second
time with task_type="execute_deployment" (see services/manager/main.py).
This graph's own `approval_gate` node/interrupt exists so DevOps has an
equivalent durable, resumable state machine if it's ever driven
standalone over NATS (mirrors the optional-standalone-service pattern
already established by services/qa/main.py and services/security/main.py).

Stages (per M3.6 spec's Deployment Workflow section):
  receive_manager_approval -> validate_qa_security
  validate_qa_security     -> generate_infrastructure
  generate_infrastructure  -> generate_cicd
  generate_cicd            -> approval_gate (INTERRUPT)
  approval_gate            -> deploy (on approval)
  deploy                   -> health_check
  health_check             -> PASS: release | FAIL: rollback
  release / rollback       -> notify_manager -> END

Supports: retry, escalation, DLQ, Postgres checkpoints — mirroring
services/qa/workflows/qa_graph.py and services/security/workflows/security_graph.py.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog
from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from services.devops.routing import (
    MAX_RETRY_CYCLES,
    route_after_approval_gate,
    route_after_deploy,
    route_after_generate_cicd,
    route_after_generate_infrastructure,
    route_after_health_check,
    route_after_rollback,
    route_after_validate_qa_security,
)

log = structlog.get_logger(__name__)


class DevOpsState(TypedDict):
    project_id:          str
    workflow_id:         str
    feature_name:        str

    qa_passed:            bool
    security_passed:      bool
    infra_ready:          bool
    cicd_ready:           bool

    approval_status:      Optional[str]   # pending|approved|rejected
    deploy_succeeded:     bool
    health_passed:        bool
    retry_cycles_run:     int

    any_dead_lettered:    bool
    dlq_tasks:            List[str]

    version:              Optional[str]
    phase_status:         str
    failure_reason:       Optional[str]
    resume_at_stage:      Optional[str]

    nats_events_queue:    List[Dict[str, Any]]
    ws_events_queue:      List[Dict[str, Any]]


async def receive_manager_approval_node(state: DevOpsState) -> Dict[str, Any]:
    log.info("devops_graph_receive", project_id=state["project_id"])
    return {
        "phase_status": "running",
        "ws_events_queue": [{
            "project_id": state["project_id"], "event_type": "phase_started",
            "payload": {"phase": 8, "phase_name": "DevOps"},
        }],
    }


async def validate_qa_security_node(state: DevOpsState) -> Dict[str, Any]:
    valid = bool(state.get("qa_passed", True)) and bool(state.get("security_passed", True))
    log.info("devops_graph_validate", project_id=state["project_id"], valid=valid)
    return {
        "phase_status": "running" if valid else "failed",
        "failure_reason": None if valid else "QA or Security gate not passed",
        "nats_events_queue": [{
            "subject": "devops.phase.started",
            "payload": {"project_id": state["project_id"], "feature_name": state.get("feature_name", "default")},
        }],
    }


async def generate_infrastructure_node(state: DevOpsState) -> Dict[str, Any]:
    log.info("devops_graph_infra", project_id=state["project_id"])
    return {"infra_ready": True}


async def generate_cicd_node(state: DevOpsState) -> Dict[str, Any]:
    log.info("devops_graph_cicd", project_id=state["project_id"])
    return {"cicd_ready": True}


async def approval_gate_node(state: DevOpsState) -> Dict[str, Any]:
    """INTERRUPT NODE — pauses for deployment approval (standalone-mode
    equivalent of Manager's own deployment_approval_gate_node)."""
    return {
        "phase_status": "awaiting_approval",
        "ws_events_queue": [{
            "project_id": state["project_id"], "event_type": "approval_required",
            "payload": {"artifact_type": "deployment_plan",
                        "message": "Deployment plan ready — please review before we deploy"},
        }],
    }


async def deploy_node(state: DevOpsState) -> Dict[str, Any]:
    log.info("devops_graph_deploy", project_id=state["project_id"])
    return {
        "nats_events_queue": [{
            "subject": "deployment.started",
            "payload": {"project_id": state["project_id"], "version": state.get("version")},
        }],
    }


async def health_check_node(state: DevOpsState) -> Dict[str, Any]:
    log.info("devops_graph_health", project_id=state["project_id"], passed=state.get("health_passed"))
    return {
        "nats_events_queue": [{
            "subject": "health.completed",
            "payload": {"project_id": state["project_id"], "passed": state.get("health_passed", False)},
        }],
    }


async def release_node(state: DevOpsState) -> Dict[str, Any]:
    log.info("devops_graph_release", project_id=state["project_id"], version=state.get("version"))
    return {
        "phase_status": "completed",
        "nats_events_queue": [
            {"subject": "deployment.completed",
             "payload": {"project_id": state["project_id"], "version": state.get("version")}},
            {"subject": "devops.phase.completed",
             "payload": {"project_id": state["project_id"], "passed": True, "status": "healthy",
                         "version": state.get("version")}},
        ],
        "ws_events_queue": [{
            "project_id": state["project_id"], "event_type": "phase_completed",
            "payload": {"phase": 8, "phase_name": "DevOps", "message": "Deployment successful"},
        }],
    }


async def rollback_node(state: DevOpsState) -> Dict[str, Any]:
    cycles = state.get("retry_cycles_run", 0) + 1
    log.info("devops_graph_rollback", project_id=state["project_id"], cycle=cycles)
    return {
        "retry_cycles_run": cycles,
        "nats_events_queue": [{
            "subject": "rollback.completed",
            "payload": {"project_id": state["project_id"], "cycle": cycles},
        }],
    }


async def notify_manager_failed_node(state: DevOpsState) -> Dict[str, Any]:
    log.info("devops_graph_notify_failed", project_id=state["project_id"])
    return {
        "phase_status": "failed",
        "nats_events_queue": [
            {"subject": "deployment.failed",
             "payload": {"project_id": state["project_id"], "reason": state.get("failure_reason", "")}},
            {"subject": "devops.phase.completed",
             "payload": {"project_id": state["project_id"], "passed": False, "status": "rolled_back"}},
        ],
        "ws_events_queue": [{
            "project_id": state["project_id"], "event_type": "phase_failed",
            "payload": {"phase": 8, "reason": state.get("failure_reason", "")},
        }],
    }


async def dlq_node(state: DevOpsState) -> Dict[str, Any]:
    log.error("devops_graph_dlq", project_id=state["project_id"], tasks=state.get("dlq_tasks", []))
    return {
        "phase_status": "failed",
        "failure_reason": f"Dead-lettered tasks: {state.get('dlq_tasks', [])}",
        "nats_events_queue": [{
            "subject": "devops.phase.failed",
            "payload": {"project_id": state["project_id"], "tasks": state.get("dlq_tasks", [])},
        }],
    }


async def handle_failure_node(state: DevOpsState) -> Dict[str, Any]:
    log.error("devops_graph_failed", project_id=state["project_id"], reason=state.get("failure_reason", ""))
    return {
        "phase_status": "failed",
        "nats_events_queue": [{
            "subject": "devops.phase.failed",
            "payload": {"project_id": state["project_id"], "reason": state.get("failure_reason", "")},
        }],
        "ws_events_queue": [{
            "project_id": state["project_id"], "event_type": "phase_failed",
            "payload": {"phase": 8, "reason": state.get("failure_reason", "")},
        }],
    }


def build_devops_graph(checkpointer=None):
    """Builds W-DEVOPS — DevOps Service LangGraph."""
    g = StateGraph(DevOpsState)

    g.add_node("receive_manager_approval", receive_manager_approval_node)
    g.add_node("validate_qa_security",     validate_qa_security_node)
    g.add_node("generate_infrastructure",  generate_infrastructure_node)
    g.add_node("generate_cicd",            generate_cicd_node)
    g.add_node("approval_gate",            approval_gate_node)
    g.add_node("deploy",                   deploy_node)
    g.add_node("health_check",             health_check_node)
    g.add_node("release",                  release_node)
    g.add_node("rollback",                 rollback_node)
    g.add_node("notify_manager_failed",    notify_manager_failed_node)
    g.add_node("dlq",                      dlq_node)
    g.add_node("handle_failure",           handle_failure_node)

    g.set_entry_point("receive_manager_approval")
    g.add_edge("receive_manager_approval", "validate_qa_security")

    g.add_conditional_edges("validate_qa_security", route_after_validate_qa_security, {
        "generate_infrastructure": "generate_infrastructure",
        "failed":                  "handle_failure",
    })

    g.add_conditional_edges("generate_infrastructure", route_after_generate_infrastructure, {
        "generate_cicd": "generate_cicd",
        "dlq":           "dlq",
        "failed":        "handle_failure",
    })

    g.add_conditional_edges("generate_cicd", route_after_generate_cicd, {
        "approval_gate": "approval_gate",
        "failed":        "handle_failure",
    })

    g.add_conditional_edges("approval_gate", route_after_approval_gate, {
        "deploy":             "deploy",
        "awaiting_approval":  "approval_gate",
        "failed":             "handle_failure",
    })

    g.add_conditional_edges("deploy", route_after_deploy, {
        "health_check": "health_check",
        "failed":       "handle_failure",
    })

    g.add_conditional_edges("health_check", route_after_health_check, {
        "release":  "release",
        "rollback": "rollback",
    })

    g.add_conditional_edges("rollback", route_after_rollback, {
        "notify_manager_failed": "notify_manager_failed",
        "failed":                "handle_failure",
    })

    g.add_edge("dlq",                   "handle_failure")
    g.add_edge("notify_manager_failed", "handle_failure")
    g.add_edge("release",               END)
    g.add_edge("handle_failure",        END)

    kwargs: Dict[str, Any] = {"interrupt_before": ["approval_gate"]}
    if checkpointer:
        kwargs["checkpointer"] = checkpointer

    return g.compile(**kwargs)
