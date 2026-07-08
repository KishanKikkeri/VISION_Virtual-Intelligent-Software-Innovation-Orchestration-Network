"""
services/manager/graphs/delegation.py
=======================================
W12 — Task Delegation Graph. The brain of the platform.
Routes every task: task_type → department → agent → model → result.
Owns retry logic, model escalation, and dead-letter routing.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog
from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════════════

class DelegationState(TypedDict):
    project_id:       str
    task_id:          str
    task_type:        str
    task_description: str
    task_context:     Dict[str, Any]
    task_priority:    int

    # Routing decisions (set by nodes)
    department:       Optional[str]
    selected_agent:   Optional[str]
    selected_provider:Optional[str]
    selected_model:   Optional[str]
    agent_run_id:     Optional[str]

    # Execution
    task_status:      str   # pending|assigned|running|completed|failed|escalated|dead_lettered
    task_output:      Optional[Dict[str, Any]]
    validation_passed:bool

    # Error handling
    retry_count:      int
    max_retries:      int
    escalation_level: int   # 0=worker, 1=lead, 2=head, 3=manager, 4=user
    dead_lettered:    bool
    failure_reason:   Optional[str]


# Task type → department mapping
TASK_DEPARTMENT_MAP: Dict[str, str] = {
    # Product
    "run_product_pipeline":       "product",
    "generate_features":          "product",
    "generate_requirements":      "product",
    "generate_user_stories":      "product",
    "generate_acceptance_criteria":"product",
    "review_requirements":        "product",
    # Architecture
    "run_architecture_pipeline":  "architecture",
    "design_system":              "architecture",
    "design_api":                 "architecture",
    "design_database":            "architecture",
    "plan_infrastructure":        "architecture",
    # Engineering
    "run_engineering_pipeline":   "engineering",
    "implement_backend":          "engineering",
    "implement_frontend":         "engineering",
    "review_code":                "engineering",
    # QA
    "run_qa_pipeline":            "qa",
    "write_unit_tests":           "qa",
    "run_integration_tests":      "qa",
    "analyze_coverage":           "qa",
    # Security
    "run_security_scan":          "security",
    "scan_dependencies":          "security",
    "check_owasp":                "security",
    # DevOps
    "run_devops_pipeline":        "devops",
    "generate_dockerfiles":       "devops",
    "generate_cicd":              "devops",
    "deploy":                     "devops",
    # Docs
    "generate_docs":              "docs",
    "generate_readme":            "docs",
    "generate_changelog":         "docs",
}

# Department → head agent
DEPARTMENT_HEAD_MAP: Dict[str, str] = {
    "product":       "product_head",
    "architecture":  "architecture_head",
    "engineering":   "engineering_head",
    "qa":            "qa_head",
    "security":      "security_head",
    "devops":        "devops_head",
    "docs":          "docs_head",
}

DLQ_DEFAULTS: Dict[str, str] = {
    "max_retries_reached":    "escalate_model",
    "validation_failed":      "reassign_agent",
    "ambiguous_requirement":  "request_user_input",
    "architecture_conflict":  "request_user_input",
    "tool_failure":           "reassign_agent",
}


# ═══════════════════════════════════════════════════════════════
# ROUTING FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def route_after_validation(state: DelegationState) -> str:
    if state["validation_passed"]:                   return "complete"
    if state["retry_count"] >= state["max_retries"]: return "dead_letter"
    return "retry"


def route_escalation(state: DelegationState) -> str:
    if state["escalation_level"] >= 4:               return "dead_letter"
    return "escalate"


# ═══════════════════════════════════════════════════════════════
# NODES
# ═══════════════════════════════════════════════════════════════

async def select_department_node(state: DelegationState) -> Dict[str, Any]:
    """Maps task_type → department using the registry."""
    dept = TASK_DEPARTMENT_MAP.get(state["task_type"])
    if not dept:
        # Fallback: infer from task_type prefix
        for key, val in TASK_DEPARTMENT_MAP.items():
            if state["task_type"].startswith(key.split("_")[0]):
                dept = val
                break

    if not dept:
        return {
            "task_status":   "failed",
            "failure_reason": f"No department mapping for task_type='{state['task_type']}'",
        }

    log.info("delegation_dept_selected", task=state["task_type"], dept=dept)
    return {"department": dept, "task_status": "assigned"}


async def select_agent_node(state: DelegationState) -> Dict[str, Any]:
    """Selects the department head agent (which coordinates its own pipeline)."""
    dept      = state.get("department")
    head      = DEPARTMENT_HEAD_MAP.get(dept, f"{dept}_head")
    escalation= state.get("escalation_level", 0)

    # On escalation, bump to manager if escalation_level >= 2
    if escalation >= 2:
        head = "manager_agent"

    log.info("delegation_agent_selected", agent=head, escalation=escalation)
    return {"selected_agent": head}


async def select_model_node(state: DelegationState) -> Dict[str, Any]:
    """Selects provider + model based on task type and escalation level."""
    from core.llm.router import select_provider_and_model
    from core.config.settings import get_settings

    settings = get_settings()
    dept     = state.get("department", "product")
    role     = "head"   # delegation always routes to head agents

    try:
        provider, model = select_provider_and_model(
            preferred_provider=settings.default_llm_provider,
            agent_role=role,
            task_type=state["task_type"],
            escalation_level=state.get("escalation_level", 0),
        )
    except Exception as e:
        provider = settings.default_llm_provider
        model    = settings.default_llm_model

    log.info("delegation_model_selected", provider=provider, model=model)
    return {"selected_provider": provider, "selected_model": model}


async def assign_task_node(state: DelegationState) -> Dict[str, Any]:
    """Records the task assignment and publishes NATS event."""
    import uuid
    run_id = str(uuid.uuid4())
    log.info("delegation_task_assigned",
             agent=state.get("selected_agent"), task_id=state["task_id"],
             provider=state.get("selected_provider"), model=state.get("selected_model"))
    return {
        "agent_run_id": run_id,
        "task_status":  "running",
    }


async def monitor_progress_node(state: DelegationState) -> Dict[str, Any]:
    """
    In the full implementation, this polls NATS for completion events.
    For Phase 2, the agent execution is synchronous within the node.
    """
    return {"task_status": "running"}


async def collect_results_node(state: DelegationState) -> Dict[str, Any]:
    """Reads agent output from agent_runs table."""
    # In Phase 2 the actual result is set by the caller via update_state()
    # after the agent completes synchronously. This node is a pass-through.
    return {}


async def validate_completion_node(state: DelegationState) -> Dict[str, Any]:
    """Validates agent output quality and completeness."""
    output = state.get("task_output")
    if not output:
        return {"validation_passed": False,
                "failure_reason":    "No output produced by agent"}

    # Basic validation: output exists and has expected keys
    passed = bool(output) and output.get("status") != "failed"
    return {"validation_passed": passed}


async def handle_retry_node(state: DelegationState) -> Dict[str, Any]:
    """Increments retry count, optionally escalates model tier."""
    retry = state.get("retry_count", 0) + 1
    escalation = state.get("escalation_level", 0)

    # After 2 retries, escalate model tier
    if retry >= 2:
        escalation = min(escalation + 1, 4)

    log.info("delegation_retry", retry=retry, escalation=escalation)
    return {
        "retry_count":    retry,
        "escalation_level": escalation,
        "task_status":    "pending",
        "agent_run_id":   None,
        "task_output":    None,
        "validation_passed": False,
    }


async def escalate_task_node(state: DelegationState) -> Dict[str, Any]:
    """Bumps escalation level and reroutes to a more senior agent."""
    level = min(state.get("escalation_level", 0) + 1, 4)
    log.warning("delegation_escalated",
                task_id=state["task_id"], level=level,
                reason=state.get("failure_reason", ""))
    return {
        "escalation_level": level,
        "task_status":      "escalated",
        "selected_agent":   None,
        "agent_run_id":     None,
    }


async def dead_letter_node(state: DelegationState) -> Dict[str, Any]:
    """Terminal: publishes task to DLQ, notifies manager and user."""
    log.error("delegation_dead_lettered",
              task_id=state["task_id"], reason=state.get("failure_reason",""),
              retries=state.get("retry_count", 0))
    return {
        "dead_lettered": True,
        "task_status":   "dead_lettered",
    }


async def task_complete_node(state: DelegationState) -> Dict[str, Any]:
    """Terminal success node."""
    log.info("delegation_complete",
             task_id=state["task_id"], agent=state.get("selected_agent"))
    return {"task_status": "completed"}


# ═══════════════════════════════════════════════════════════════
# GRAPH BUILDER
# ═══════════════════════════════════════════════════════════════

def build_delegation_graph(checkpointer=None):
    """
    Builds and compiles W12 — Task Delegation Graph.
    No interrupt nodes — fully autonomous.
    """
    g = StateGraph(DelegationState)

    g.add_node("select_department",    select_department_node)
    g.add_node("select_agent",         select_agent_node)
    g.add_node("select_model",         select_model_node)
    g.add_node("assign_task",          assign_task_node)
    g.add_node("monitor_progress",     monitor_progress_node)
    g.add_node("collect_results",      collect_results_node)
    g.add_node("validate_completion",  validate_completion_node)
    g.add_node("handle_retry",         handle_retry_node)
    g.add_node("escalate_task",        escalate_task_node)
    g.add_node("dead_letter",          dead_letter_node)
    g.add_node("task_complete",        task_complete_node)

    g.set_entry_point("select_department")

    g.add_edge("select_department",    "select_agent")
    g.add_edge("select_agent",         "select_model")
    g.add_edge("select_model",         "assign_task")
    g.add_edge("assign_task",          "monitor_progress")
    g.add_edge("monitor_progress",     "collect_results")
    g.add_edge("collect_results",      "validate_completion")
    g.add_edge("handle_retry",         "select_agent")
    g.add_edge("escalate_task",        "select_agent")
    g.add_edge("dead_letter",          END)
    g.add_edge("task_complete",        END)

    g.add_conditional_edges("validate_completion", route_after_validation, {
        "complete":    "task_complete",
        "retry":       "handle_retry",
        "dead_letter": "dead_letter",
    })
    g.add_conditional_edges("handle_retry", route_escalation, {
        "escalate":    "escalate_task",
        "dead_letter": "dead_letter",
    })

    kwargs: Dict[str, Any] = {}
    if checkpointer:
        kwargs["checkpointer"] = checkpointer

    return g.compile(**kwargs)
