"""
services/manager/graphs/lifecycle.py
======================================
W01 — Project Lifecycle Graph.
The master state machine. Manager Agent is the sole authority.
3 interrupt nodes: requirements, architecture, deployment approvals.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

import structlog
from langgraph.graph import END, StateGraph
from langgraph.types import Send
from typing_extensions import TypedDict

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════════════

class LifecycleState(TypedDict):
    # Identity
    project_id:        str
    workflow_id:       str
    owner_id:          str

    # Phase (manager writes only)
    current_phase:     int
    phase_status:      str    # pending|running|awaiting_approval|completed|failed

    # Tasks
    active_tasks:      List[Dict[str, Any]]
    completed_tasks:   List[Dict[str, Any]]
    failed_tasks:      List[Dict[str, Any]]

    # Artifacts
    artifacts:         Dict[str, str]   # type → artifact_id

    # Approval gate
    awaiting_approval:      bool
    approval_artifact_type: Optional[str]
    approval_status:        Optional[str]   # pending|approved|rejected
    approval_feedback:      Optional[str]
    revision_round:         int

    # Budget
    budget_limit_usd:  Optional[float]
    total_spend_usd:   float
    budget_status:     str    # active|warning|exceeded

    # Error handling
    retry_count:       int
    failure_reason:    Optional[str]
    escalation_required: bool

    # Event queues (flushed at end of each node)
    nats_events_queue:      List[Dict[str, Any]]
    websocket_events_queue: List[Dict[str, Any]]


# ═══════════════════════════════════════════════════════════════
# ROUTING FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def route_requirements_approval(state: LifecycleState) -> str:
    if state["budget_status"] == "exceeded":       return "budget_exceeded"
    if state["revision_round"] >= 5:               return "max_revisions"
    if state["approval_status"] == "approved":     return "approved"
    if state["approval_status"] == "rejected":     return "rejected"
    return "pending"


def route_architecture_approval(state: LifecycleState) -> str:
    if state["budget_status"] == "exceeded":       return "budget_exceeded"
    if state["revision_round"] >= 5:               return "max_revisions"
    if state["approval_status"] == "approved":     return "approved"
    if state["approval_status"] == "rejected":     return "rejected"
    return "pending"


def route_after_implementation(state: LifecycleState) -> str:
    if state["budget_status"] == "exceeded":       return "budget_exceeded"
    if state["phase_status"] == "failed":          return "failed"
    return "complete"


def route_validation(state: LifecycleState) -> str:
    if state["budget_status"] == "exceeded":       return "budget_exceeded"
    security_blocked = any(t.get("type") == "security_block" for t in state["failed_tasks"])
    if security_blocked:                           return "security_blocked"
    qa_failed = any(t.get("type") == "qa_failure" for t in state["failed_tasks"])
    if qa_failed:                                  return "qa_failed"
    return "all_passed"


def route_deployment_approval(state: LifecycleState) -> str:
    if state["budget_status"] == "exceeded":       return "budget_exceeded"
    if state["approval_status"] == "approved":     return "approved"
    if state["approval_status"] == "rejected":     return "rejected"
    return "pending"


def route_after_deployment(state: LifecycleState) -> str:
    if state["phase_status"] == "failed":          return "failed"
    return "success"


# ═══════════════════════════════════════════════════════════════
# NODE IMPLEMENTATIONS
# ═══════════════════════════════════════════════════════════════

async def project_intake_node(state: LifecycleState) -> Dict[str, Any]:
    """
    Phase 1: Validates project and initialises workflow state.
    Publishes manager.project.created to NATS.
    """
    log.info("lifecycle_intake", project_id=state["project_id"])
    return {
        "current_phase": 1,
        "phase_status":  "completed",
        "retry_count":   0,
        "revision_round":0,
        "budget_status": "active",
        "awaiting_approval": False,
        "nats_events_queue": [{
            "subject": "manager.project.created",
            "payload": {"project_id": state["project_id"], "phase": 1},
        }],
        "websocket_events_queue": [{
            "project_id": state["project_id"],
            "event_type": "phase_changed",
            "payload":    {"from_phase": 0, "to_phase": 1, "status": "Project created"},
        }],
    }


async def requirements_phase_node(state: LifecycleState) -> Dict[str, Any]:
    """
    Phase 2: Publishes task assignment to product-service.
    Waits for product.requirements.completed via NATS.
    The actual waiting is handled by the NATS subscriber in the service layer.
    """
    log.info("lifecycle_requirements", project_id=state["project_id"])
    return {
        "current_phase": 2,
        "phase_status":  "running",
        "nats_events_queue": [{
            "subject": "manager.task.assigned",
            "payload": {
                "project_id": state["project_id"],
                "department": "product",
                "task_type":  "run_product_pipeline",
            },
        }],
        "websocket_events_queue": [{
            "project_id": state["project_id"],
            "event_type": "phase_started",
            "payload":    {"phase": 2, "phase_name": "Requirements Analysis"},
        }],
    }


async def requirements_approval_gate_node(state: LifecycleState) -> Dict[str, Any]:
    """
    INTERRUPT NODE — graph pauses here.
    Compiled with interrupt_before=["requirements_approval_gate"].
    Resumes when manager-service calls graph.update_state() with approval decision.
    """
    log.info("lifecycle_approval_gate",
             project_id=state["project_id"], gate="requirements")
    return {
        "phase_status":         "awaiting_approval",
        "awaiting_approval":    True,
        "approval_artifact_type": "requirements",
        "websocket_events_queue": [{
            "project_id": state["project_id"],
            "event_type": "approval_required",
            "payload":    {
                "artifact_type": "requirements",
                "message":       "Requirements package ready for your review and approval",
                "revision_round":state.get("revision_round", 0),
            },
        }],
    }


async def requirements_revision_node(state: LifecycleState) -> Dict[str, Any]:
    """Routes rejection feedback back to product-service for revision."""
    round_ = state.get("revision_round", 0) + 1
    log.info("lifecycle_revision", project_id=state["project_id"], round=round_)
    return {
        "revision_round":  round_,
        "approval_status": None,
        "awaiting_approval": False,
        "phase_status":    "running",
        "nats_events_queue": [{
            "subject": "manager.approval.rejected",
            "payload": {
                "project_id":       state["project_id"],
                "artifact_type":    "requirements",
                "revision_feedback":state.get("approval_feedback", ""),
                "revision_round":   round_,
            },
        }],
        "websocket_events_queue": [{
            "project_id": state["project_id"],
            "event_type": "revision_requested",
            "payload":    {"round": round_, "feedback": state.get("approval_feedback", "")},
        }],
    }


async def architecture_phase_node(state: LifecycleState) -> Dict[str, Any]:
    log.info("lifecycle_architecture", project_id=state["project_id"])
    return {
        "current_phase": 3,
        "phase_status":  "running",
        "nats_events_queue": [{
            "subject": "manager.task.assigned",
            "payload": {"project_id": state["project_id"], "department": "architecture"},
        }],
        "websocket_events_queue": [{
            "project_id": state["project_id"],
            "event_type": "phase_started",
            "payload":    {"phase": 3, "phase_name": "Architecture Design"},
        }],
    }


async def architecture_approval_gate_node(state: LifecycleState) -> Dict[str, Any]:
    """INTERRUPT NODE — pauses for architecture approval."""
    return {
        "phase_status":           "awaiting_approval",
        "awaiting_approval":      True,
        "approval_artifact_type": "architecture",
        "websocket_events_queue": [{
            "project_id": state["project_id"],
            "event_type": "approval_required",
            "payload":    {"artifact_type": "architecture",
                           "message": "Architecture blueprint ready for your review"},
        }],
    }


async def architecture_revision_node(state: LifecycleState) -> Dict[str, Any]:
    round_ = state.get("revision_round", 0) + 1
    return {
        "revision_round":  round_,
        "approval_status": None,
        "awaiting_approval": False,
        "phase_status":    "running",
        "nats_events_queue": [{
            "subject": "manager.approval.rejected",
            "payload": {"project_id": state["project_id"],
                        "artifact_type": "architecture",
                        "revision_feedback": state.get("approval_feedback",""),
                        "revision_round": round_},
        }],
    }


async def project_structure_node(state: LifecycleState) -> Dict[str, Any]:
    log.info("lifecycle_project_structure", project_id=state["project_id"])
    return {
        "current_phase": 4,
        "phase_status":  "completed",
        "websocket_events_queue": [{
            "project_id": state["project_id"],
            "event_type": "phase_started",
            "payload":    {"phase": 4, "phase_name": "Project Structure Generation"},
        }],
    }


async def implementation_phase_node(state: LifecycleState) -> Dict[str, Any]:
    log.info("lifecycle_implementation", project_id=state["project_id"])
    return {
        "current_phase": 5,
        "phase_status":  "running",
        "nats_events_queue": [{
            "subject": "manager.task.assigned",
            "payload": {"project_id": state["project_id"], "department": "engineering"},
        }],
        "websocket_events_queue": [{
            "project_id": state["project_id"],
            "event_type": "phase_started",
            "payload":    {"phase": 5, "phase_name": "Implementation"},
        }],
    }


async def validation_phase_node(state: LifecycleState) -> Dict[str, Any]:
    """Fans out QA + Security + Docs in parallel via Send()."""
    log.info("lifecycle_validation", project_id=state["project_id"])
    return {
        "current_phase": 6,
        "phase_status":  "running",
        "websocket_events_queue": [{
            "project_id": state["project_id"],
            "event_type": "phase_started",
            "payload":    {"phase": 6, "phase_name": "Testing + Security (parallel)"},
        }],
    }


async def engineering_rework_node(state: LifecycleState) -> Dict[str, Any]:
    return {
        "phase_status":  "running",
        "nats_events_queue": [{
            "subject": "manager.task.assigned",
            "payload": {"project_id": state["project_id"], "department": "engineering",
                        "task_type": "rework", "failed_tasks": state.get("failed_tasks", [])},
        }],
    }


async def deployment_phase_node(state: LifecycleState) -> Dict[str, Any]:
    log.info("lifecycle_deployment", project_id=state["project_id"])
    return {
        "current_phase": 8,
        "phase_status":  "running",
        "nats_events_queue": [{
            "subject": "manager.task.assigned",
            "payload": {"project_id": state["project_id"], "department": "devops"},
        }],
    }


async def deployment_approval_gate_node(state: LifecycleState) -> Dict[str, Any]:
    """INTERRUPT NODE — pauses for deployment approval."""
    return {
        "phase_status":           "awaiting_approval",
        "awaiting_approval":      True,
        "approval_artifact_type": "deployment_plan",
        "websocket_events_queue": [{
            "project_id": state["project_id"],
            "event_type": "approval_required",
            "payload":    {"artifact_type": "deployment_plan",
                           "message": "Deployment plan ready — please review before we deploy"},
        }],
    }


async def execute_deployment_phase_node(state: LifecycleState) -> Dict[str, Any]:
    return {"phase_status": "running", "current_phase": 8}


async def monitoring_phase_node(state: LifecycleState) -> Dict[str, Any]:
    return {
        "current_phase": 9,
        "phase_status":  "running",
        "websocket_events_queue": [{
            "project_id": state["project_id"],
            "event_type": "phase_started",
            "payload":    {"phase": 9, "phase_name": "Production Monitoring"},
        }],
    }


async def budget_exceeded_handler_node(state: LifecycleState) -> Dict[str, Any]:
    log.warning("lifecycle_budget_exceeded", project_id=state["project_id"],
                spend=state.get("total_spend_usd"), limit=state.get("budget_limit_usd"))
    return {
        "phase_status": "paused",
        "nats_events_queue": [{
            "subject": "manager.budget.exceeded",
            "payload": {"project_id": state["project_id"],
                        "spend": state.get("total_spend_usd", 0),
                        "limit": state.get("budget_limit_usd")},
        }],
        "websocket_events_queue": [{
            "project_id": state["project_id"],
            "event_type": "budget_exceeded",
            "payload":    {"spend": state.get("total_spend_usd", 0),
                           "limit": state.get("budget_limit_usd"),
                           "message": "Project paused — budget limit reached"},
        }],
    }


async def handle_failure_node(state: LifecycleState) -> Dict[str, Any]:
    log.error("lifecycle_failed", project_id=state["project_id"],
              reason=state.get("failure_reason"))
    return {
        "phase_status":    "failed",
        "nats_events_queue": [{
            "subject": "manager.project.failed",
            "payload": {"project_id": state["project_id"],
                        "reason": state.get("failure_reason", "Unknown failure")},
        }],
        "websocket_events_queue": [{
            "project_id": state["project_id"],
            "event_type": "project_failed",
            "payload":    {"reason": state.get("failure_reason", "Unknown failure")},
        }],
    }


async def project_complete_node(state: LifecycleState) -> Dict[str, Any]:
    log.info("lifecycle_complete", project_id=state["project_id"])
    return {
        "current_phase": 10,
        "phase_status":  "completed",
        "nats_events_queue": [{
            "subject": "manager.project.completed",
            "payload": {"project_id": state["project_id"]},
        }],
        "websocket_events_queue": [{
            "project_id": state["project_id"],
            "event_type": "project_completed",
            "payload":    {"message": "Project completed successfully!",
                           "artifacts": state.get("artifacts", {})},
        }],
    }


async def max_revisions_node(state: LifecycleState) -> Dict[str, Any]:
    return {
        "failure_reason": f"Maximum revision rounds reached ({state.get('revision_round', 0)})",
        "phase_status":   "failed",
    }


# ═══════════════════════════════════════════════════════════════
# GRAPH BUILDER
# ═══════════════════════════════════════════════════════════════

def build_lifecycle_graph(checkpointer=None):
    """
    Builds and compiles W01 — Project Lifecycle Graph.
    3 interrupt nodes: requirements, architecture, deployment approvals.

    Args:
        checkpointer: PostgresSaver instance (None for in-memory testing)
    """
    g = StateGraph(LifecycleState)

    # Register all nodes
    g.add_node("project_intake",              project_intake_node)
    g.add_node("requirements_phase",          requirements_phase_node)
    g.add_node("requirements_approval_gate",  requirements_approval_gate_node)
    g.add_node("requirements_revision",       requirements_revision_node)
    g.add_node("architecture_phase",          architecture_phase_node)
    g.add_node("architecture_approval_gate",  architecture_approval_gate_node)
    g.add_node("architecture_revision",       architecture_revision_node)
    g.add_node("project_structure",           project_structure_node)
    g.add_node("implementation_phase",        implementation_phase_node)
    g.add_node("validation_phase",            validation_phase_node)
    g.add_node("engineering_rework",          engineering_rework_node)
    g.add_node("deployment_phase",            deployment_phase_node)
    g.add_node("deployment_approval_gate",    deployment_approval_gate_node)
    g.add_node("execute_deployment",          execute_deployment_phase_node)
    g.add_node("monitoring_phase",            monitoring_phase_node)
    g.add_node("budget_exceeded_handler",     budget_exceeded_handler_node)
    g.add_node("handle_failure",              handle_failure_node)
    g.add_node("project_complete",            project_complete_node)
    g.add_node("max_revisions",               max_revisions_node)

    # Entry
    g.set_entry_point("project_intake")

    # Linear edges
    g.add_edge("project_intake",             "requirements_phase")
    g.add_edge("requirements_phase",         "requirements_approval_gate")
    g.add_edge("requirements_revision",      "requirements_phase")
    g.add_edge("architecture_phase",         "architecture_approval_gate")
    g.add_edge("architecture_revision",      "architecture_phase")
    g.add_edge("project_structure",          "implementation_phase")
    g.add_edge("engineering_rework",         "implementation_phase")
    g.add_edge("monitoring_phase",           "project_complete")
    g.add_edge("max_revisions",              "handle_failure")
    g.add_edge("project_complete",           END)
    g.add_edge("handle_failure",             END)
    g.add_edge("budget_exceeded_handler",    END)

    # Requirements approval gate
    g.add_conditional_edges("requirements_approval_gate", route_requirements_approval, {
        "approved":       "architecture_phase",
        "rejected":       "requirements_revision",
        "max_revisions":  "max_revisions",
        "budget_exceeded":"budget_exceeded_handler",
        "pending":        "requirements_approval_gate",
    })

    # Architecture approval gate
    g.add_conditional_edges("architecture_approval_gate", route_architecture_approval, {
        "approved":       "project_structure",
        "rejected":       "architecture_revision",
        "max_revisions":  "max_revisions",
        "budget_exceeded":"budget_exceeded_handler",
        "pending":        "architecture_approval_gate",
    })

    # Implementation completion
    g.add_conditional_edges("implementation_phase", route_after_implementation, {
        "complete":       "validation_phase",
        "failed":         "handle_failure",
        "budget_exceeded":"budget_exceeded_handler",
    })

    # Validation (QA + Security parallel)
    g.add_conditional_edges("validation_phase", route_validation, {
        "all_passed":     "deployment_phase",
        "qa_failed":      "engineering_rework",
        "security_blocked":"handle_failure",
        "budget_exceeded":"budget_exceeded_handler",
    })

    # Deployment approval gate
    g.add_edge("deployment_phase",           "deployment_approval_gate")
    g.add_conditional_edges("deployment_approval_gate", route_deployment_approval, {
        "approved":       "execute_deployment",
        "rejected":       "deployment_phase",
        "budget_exceeded":"budget_exceeded_handler",
        "pending":        "deployment_approval_gate",
    })

    # Post-deployment
    g.add_conditional_edges("execute_deployment", route_after_deployment, {
        "success": "monitoring_phase",
        "failed":  "handle_failure",
    })

    compile_kwargs: Dict[str, Any] = {
        "interrupt_before": [
            "requirements_approval_gate",
            "architecture_approval_gate",
            "deployment_approval_gate",
        ],
    }
    if checkpointer:
        compile_kwargs["checkpointer"] = checkpointer

    return g.compile(**compile_kwargs)
