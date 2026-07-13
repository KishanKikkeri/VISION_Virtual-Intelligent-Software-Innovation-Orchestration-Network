"""
services/architecture/workflows/architecture_graph.py
=======================================================
W-Arch: Architecture Service LangGraph State Machine.

Phases:
  load_requirements    → reads approved product artifacts
  system_design        → blueprint + api_spec + db_schema (sequential)
  parallel_design      → infra + security + scaling + integration (Send() fan-out)
  traceability         → maps every requirement through the stack
  architecture_review  → cross-cutting consistency check
  artifact_packaging   → status: draft → under_review
  approval_gate        → INTERRUPT NODE — pauses for user
  publish_artifacts    → status → approved, NATS event fired

The ArchitectureHead agent handles all business logic.
This graph owns routing and durability (PostgresSaver checkpointing).
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional
import structlog
from langgraph.graph import END, StateGraph
from langgraph.types import Send
from typing_extensions import TypedDict
log = structlog.get_logger(__name__)


class ArchitectureState(TypedDict):
    project_id:          str
    workflow_id:         str
    requirements_ready:  bool

    # Artifact tracking
    blueprint_ready:     bool
    api_spec_ready:      bool
    db_schema_ready:     bool
    deployment_ready:    bool
    security_ready:      bool
    scaling_ready:       bool
    integration_ready:   bool
    traceability_ready:  bool
    review_passed:       bool

    # Coverage
    coverage_pct:        float
    traceability_reruns: int

    # Approval gate
    awaiting_approval:   bool
    approval_status:     Optional[str]
    approval_feedback:   Optional[str]
    revision_round:      int

    # Artifacts produced
    artifacts:           Dict[str, str]   # type → artifact_id

    # Error handling
    failure_reason:      Optional[str]
    phase_status:        str

    # Event queues (flushed after each node)
    nats_events_queue:   List[Dict[str, Any]]
    ws_events_queue:     List[Dict[str, Any]]


# ── Routing ───────────────────────────────────────────────────

def route_after_system_design(state: ArchitectureState) -> str:
    if state["phase_status"] == "failed": return "failed"
    return "parallel"

def route_after_parallel(state: ArchitectureState) -> str:
    if state["phase_status"] == "failed": return "failed"
    return "traceability"

def route_after_traceability(state: ArchitectureState) -> str:
    if state["phase_status"] == "failed": return "failed"
    if state["coverage_pct"] < 80.0:
        if state["traceability_reruns"] < 2: return "traceability_rerun"
        return "failed"
    return "review"

def route_after_review(state: ArchitectureState) -> str:
    if not state.get("review_passed", False): return "failed"
    return "package"

def route_approval_gate(state: ArchitectureState) -> str:
    if state["approval_status"] == "approved":  return "approved"
    if state["approval_status"] == "rejected":  return "rejected"
    if state["revision_round"] >= 5:            return "max_revisions"
    return "pending"

def fan_out_parallel_design(state: ArchitectureState) -> List[Send]:
    """Fan-out to 4 parallel design tasks via LangGraph Send()."""
    workers = [
        ("parallel_infra_node",       "Infrastructure & deployment"),
        ("parallel_security_node",    "Security architecture"),
        ("parallel_scaling_node",     "Scaling strategy"),
        ("parallel_integration_node", "Integration plan"),
    ]
    return [Send(node, {**state, "parallel_task": task}) for node, task in workers]


# ── Nodes ─────────────────────────────────────────────────────

async def load_requirements_node(state: ArchitectureState) -> Dict[str, Any]:
    log.info("arch_graph_load_requirements", project_id=state["project_id"])
    return {
        "requirements_ready": True,
        "phase_status":       "running",
        "ws_events_queue": [{
            "project_id": state["project_id"],
            "event_type": "phase_started",
            "payload":    {"phase": 3, "phase_name": "Architecture Design"},
        }],
    }

async def system_design_node(state: ArchitectureState) -> Dict[str, Any]:
    """Runs SystemDesignLead (blueprint → api_spec → db_schema, sequential)."""
    log.info("arch_graph_system_design", project_id=state["project_id"])
    return {
        "blueprint_ready": True,
        "api_spec_ready":  True,
        "db_schema_ready": True,
        "nats_events_queue": [{
            "subject": "architecture.system_design.started",
            "payload": {"project_id": state["project_id"]},
        }],
        "ws_events_queue": [{
            "project_id": state["project_id"],
            "event_type": "agent_started",
            "payload":    {"agent": "system_design_lead", "step": "Core design pipeline"},
        }],
    }

async def parallel_infra_node(state: ArchitectureState) -> Dict[str, Any]:
    return {"deployment_ready": True}

async def parallel_security_node(state: ArchitectureState) -> Dict[str, Any]:
    return {"security_ready": True}

async def parallel_scaling_node(state: ArchitectureState) -> Dict[str, Any]:
    return {"scaling_ready": True}

async def parallel_integration_node(state: ArchitectureState) -> Dict[str, Any]:
    return {"integration_ready": True}

async def collect_parallel_node(state: ArchitectureState) -> Dict[str, Any]:
    """Collects parallel design results. All 4 must be ready."""
    all_ready = (state.get("deployment_ready") and state.get("security_ready")
                 and state.get("scaling_ready")  and state.get("integration_ready"))
    return {
        "phase_status": "running" if all_ready else "failed",
        "failure_reason": None if all_ready else "One or more parallel design workers failed",
        "nats_events_queue": [{
            "subject": "architecture.platform_design.completed",
            "payload": {"project_id": state["project_id"], "all_ready": all_ready},
        }],
    }

async def traceability_node(state: ArchitectureState) -> Dict[str, Any]:
    log.info("arch_graph_traceability", project_id=state["project_id"])
    # Coverage is calculated by TraceabilityAgent; default 90% for graph state
    return {
        "traceability_ready": True,
        "coverage_pct":       90.0,   # updated by agent result injection
        "ws_events_queue": [{
            "project_id": state["project_id"],
            "event_type": "traceability_check",
            "payload":    {"status": "running"},
        }],
    }

async def traceability_rerun_node(state: ArchitectureState) -> Dict[str, Any]:
    reruns = state.get("traceability_reruns", 0) + 1
    log.warning("arch_graph_traceability_rerun",
                project_id=state["project_id"], attempt=reruns)
    return {
        "traceability_reruns": reruns,
        "traceability_ready":  False,
        "ws_events_queue": [{
            "project_id": state["project_id"],
            "event_type": "traceability_gap_rerun",
            "payload":    {"attempt": reruns},
        }],
    }

async def architecture_review_node(state: ArchitectureState) -> Dict[str, Any]:
    log.info("arch_graph_review", project_id=state["project_id"])
    return {
        "review_passed": True,   # updated by agent result injection
        "nats_events_queue": [{
            "subject": "architecture.review.started",
            "payload": {"project_id": state["project_id"]},
        }],
    }

async def artifact_packaging_node(state: ArchitectureState) -> Dict[str, Any]:
    log.info("arch_graph_packaging", project_id=state["project_id"])
    return {
        "phase_status":      "awaiting_approval",
        "awaiting_approval": True,
        "nats_events_queue": [{
            "subject": "architecture.approval.requested",
            "payload": {"project_id": state["project_id"],
                        "artifact_types": ["architecture_blueprint","api_spec",
                                           "database_schema","deployment_architecture","ui_blueprint"]},
        }],
    }

async def approval_gate_node(state: ArchitectureState) -> Dict[str, Any]:
    """INTERRUPT NODE — graph pauses here until manager injects approval decision."""
    return {
        "awaiting_approval": True,
        "ws_events_queue": [{
            "project_id": state["project_id"],
            "event_type": "approval_required",
            "payload":    {
                "artifact_type": "architecture",
                "message":       "Architecture blueprint ready for your review and approval",
                "artifacts":     ["architecture_blueprint","api_spec",
                                  "database_schema","deployment_architecture","ui_blueprint"],
            },
        }],
    }

async def publish_artifacts_node(state: ArchitectureState) -> Dict[str, Any]:
    log.info("arch_graph_publish", project_id=state["project_id"])
    return {
        "awaiting_approval": False,
        "phase_status":      "completed",
        "nats_events_queue": [{
            "subject": "architecture.design.completed",
            "payload": {"project_id": state["project_id"],
                        "requires_approval": False,   # already approved
                        "phase": 3},
        }],
        "ws_events_queue": [{
            "project_id": state["project_id"],
            "event_type": "phase_completed",
            "payload":    {"phase": 3, "phase_name": "Architecture Design",
                           "message": "Architecture approved — moving to project structure"},
        }],
    }

async def revision_node(state: ArchitectureState) -> Dict[str, Any]:
    rev = state.get("revision_round", 0) + 1
    return {
        "revision_round":    rev,
        "approval_status":   None,
        "awaiting_approval": False,
        "phase_status":      "running",
        "nats_events_queue": [{
            "subject": "architecture.design.revised",
            "payload": {"project_id": state["project_id"],
                        "revision_round": rev,
                        "feedback": state.get("approval_feedback","")},
        }],
        "ws_events_queue": [{
            "project_id": state["project_id"],
            "event_type": "revision_started",
            "payload":    {"round": rev, "feedback": state.get("approval_feedback","")},
        }],
    }

async def max_revisions_node(state: ArchitectureState) -> Dict[str, Any]:
    return {"phase_status":"failed", "failure_reason":f"Max revision rounds ({state.get('revision_round',0)}) reached"}

async def handle_failure_node(state: ArchitectureState) -> Dict[str, Any]:
    log.error("arch_graph_failed", project_id=state["project_id"],
              reason=state.get("failure_reason",""))
    return {
        "phase_status": "failed",
        "nats_events_queue": [{
            "subject": "architecture.pipeline.failed",
            "payload": {"project_id": state["project_id"],
                        "reason": state.get("failure_reason","")},
        }],
        "ws_events_queue": [{
            "project_id": state["project_id"],
            "event_type": "phase_failed",
            "payload":    {"phase": 3, "reason": state.get("failure_reason","")},
        }],
    }


# ── Graph builder ──────────────────────────────────────────────

def build_architecture_graph(checkpointer=None):
    """
    Builds W-Arch — Architecture Service LangGraph.
    1 interrupt node: approval_gate.
    Parallel phase: 4 Send() workers for platform design.
    """
    g = StateGraph(ArchitectureState)

    # Register nodes
    g.add_node("load_requirements",    load_requirements_node)
    g.add_node("system_design",        system_design_node)
    g.add_node("parallel_infra",       parallel_infra_node)
    g.add_node("parallel_security",    parallel_security_node)
    g.add_node("parallel_scaling",     parallel_scaling_node)
    g.add_node("parallel_integration", parallel_integration_node)
    g.add_node("collect_parallel",     collect_parallel_node)
    g.add_node("traceability",         traceability_node)
    g.add_node("traceability_rerun",   traceability_rerun_node)
    g.add_node("architecture_review",  architecture_review_node)
    g.add_node("artifact_packaging",   artifact_packaging_node)
    g.add_node("approval_gate",        approval_gate_node)
    g.add_node("publish_artifacts",    publish_artifacts_node)
    g.add_node("revision",             revision_node)
    g.add_node("max_revisions",        max_revisions_node)
    g.add_node("handle_failure",       handle_failure_node)

    # Entry
    g.set_entry_point("load_requirements")

    # Linear edges
    g.add_edge("load_requirements",    "system_design")
    g.add_edge("parallel_infra",       "collect_parallel")
    g.add_edge("parallel_security",    "collect_parallel")
    g.add_edge("parallel_scaling",     "collect_parallel")
    g.add_edge("parallel_integration", "collect_parallel")
    g.add_edge("artifact_packaging",   "approval_gate")
    g.add_edge("revision",             "system_design")   # re-run from system design on rejection
    g.add_edge("max_revisions",        "handle_failure")
    g.add_edge("publish_artifacts",    END)
    g.add_edge("handle_failure",       END)
    g.add_edge("traceability_rerun",   "traceability")

    # Conditional edges
    g.add_conditional_edges("system_design", route_after_system_design, {
        "parallel": "parallel_infra",   # fan-out node triggers Send() to all 4
        "failed":   "handle_failure",
    })
    g.add_conditional_edges("collect_parallel", route_after_parallel, {
        "traceability": "traceability",
        "failed":       "handle_failure",
    })
    g.add_conditional_edges("traceability", route_after_traceability, {
        "review":             "architecture_review",
        "traceability_rerun": "traceability_rerun",
        "failed":             "handle_failure",
    })
    g.add_conditional_edges("architecture_review", route_after_review, {
        "package": "artifact_packaging",
        "failed":  "handle_failure",
    })
    g.add_conditional_edges("approval_gate", route_approval_gate, {
        "approved":      "publish_artifacts",
        "rejected":      "revision",
        "max_revisions": "max_revisions",
        "pending":       "approval_gate",
    })

    kwargs = {"interrupt_before": ["approval_gate"]}
    if checkpointer:
        kwargs["checkpointer"] = checkpointer

    return g.compile(**kwargs)
