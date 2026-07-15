"""
services/incident_response/workflows/incident_response_graph.py
=======================================================================
W-INCIDENT-RESPONSE: Incident Response Service LangGraph state machine.

Bounded, event-driven model (deviation from Monitoring's continuous
scheduler, spec §10 "No infinite loops. Scheduler belongs only to
Monitoring."): one compiled invocation is exactly one incident's full
lifecycle, intake -> analyze -> [recover?] -> communicate -> finalize
-> END. There is no re-invocation loop here — services/incident_response/
main.py's NATS subscriber (api/events.py) invokes this graph exactly
once per `monitoring.incident` event, using the incident_id as the
thread_id (so a second event for the same incident_id, if Monitoring
ever re-raises it, resumes rather than duplicates).

Every node that needs a decision calls `factory.create(agent_id).run(task)`
directly, mirroring services/monitoring/workflows/monitoring_graph.py's
nodes (not services/devops/workflows/devops_graph.py's pure
state-transition-marker nodes) — a per-incident workflow needs its own
durable, resumable execution engine just like a per-cycle one does.
The `intake` node is the one exception: it only persists the incoming
event's fields, no agent judgment is needed yet, so it's a plain node
(same reasoning as monitoring_graph.py's `incident_handoff_node` being
a plain marker node).
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

import structlog
from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from core.runtime.context import AgentContext, TaskInput
from services.incident_response.routing import (
    route_after_analyze,
    route_after_communicate,
    route_after_intake,
    route_after_recover,
)

log = structlog.get_logger(__name__)


class IncidentResponseState(TypedDict):
    incident_id:         str
    project_id:           str   # sentinel platform-anchor project_id (or the correlated project once known)
    workflow_id:          str

    component:            str
    severity:             str
    breach_cycles:        int

    recommended_action:   Optional[str]
    recovery_status:       Optional[str]
    final_status:          Optional[str]

    phase_status:          str
    failure_reason:        Optional[str]


def _new_task(state: IncidentResponseState, agent_id: str, task_type: str) -> TaskInput:
    ctx = AgentContext(
        project_id=state["project_id"], workflow_id=state["workflow_id"],
        current_phase=1, project_name="Platform Incident Response",
        project_description="Sentinel context for one Incident Response Service lifecycle.",
    )
    ctx.approved_artifacts["__incident_id__"] = state["incident_id"]
    ctx.approved_artifacts["__component__"] = state["component"]
    ctx.approved_artifacts["__severity__"] = state["severity"]
    ctx.approved_artifacts["__breach_cycles__"] = state.get("breach_cycles", 1)

    return TaskInput.create(
        project_id=state["project_id"], agent_id=agent_id, parent_agent_id="incident_response_head",
        task_type=task_type, description=f"Incident {state['incident_id']}: {task_type}",
        expected_output="AgentResult.content per services/incident_response/models.py", context=ctx,
    )


def build_incident_response_graph(factory: Any, checkpointer: Any = None):
    """
    Builds W-INCIDENT-RESPONSE. `factory` is an AgentFactory instance
    (or None for dry-run/tests, in which case nodes degrade to no-ops —
    mirrors services/monitoring/workflows/monitoring_graph.py's
    `if not factory` fallback).
    """

    async def intake_node(state: IncidentResponseState) -> Dict[str, Any]:
        log.info("incident_response_graph_intake", incident_id=state["incident_id"],
                 component=state["component"], severity=state["severity"])
        return {"phase_status": "running"}

    async def analyze_node(state: IncidentResponseState) -> Dict[str, Any]:
        log.info("incident_response_graph_analyze", incident_id=state["incident_id"])
        if not factory:
            return {"recommended_action": "none", "phase_status": "running"}

        task = _new_task(state, "incident_analysis_lead", "analyze")
        task.context.approved_artifacts["__factory__"] = factory
        result = await factory.create("incident_analysis_lead").run(task)

        content = result.content or {}
        return {
            "recommended_action": content.get("recommended_action", "none"),
            "phase_status": "running" if result.status.value != "failed" else "degraded",
        }

    async def recover_node(state: IncidentResponseState) -> Dict[str, Any]:
        log.info("incident_response_graph_recover", incident_id=state["incident_id"])
        if not factory:
            return {"recovery_status": "skipped"}

        task = _new_task(state, "recovery_lead", "recover")
        task.context.approved_artifacts["__factory__"] = factory
        result = await factory.create("recovery_lead").run(task)

        content = result.content or {}
        return {
            "recovery_status": content.get("recovery_status", "skipped"),
            "phase_status": "running" if result.status.value != "failed" else "degraded",
        }

    async def communicate_node(state: IncidentResponseState) -> Dict[str, Any]:
        log.info("incident_response_graph_communicate", incident_id=state["incident_id"])
        if not factory:
            return {"final_status": "monitoring"}

        task = _new_task(state, "communication_lead", "communicate")
        task.context.approved_artifacts["__factory__"] = factory
        # recommended_action/recovery_status from prior nodes must be visible
        # to Reporting Worker's final-status computation (utils.final_status_for).
        task.context.approved_artifacts["incident_classifier_worker"] = {
            "classification": {"recommended_action": state.get("recommended_action", "none")},
        }
        task.context.approved_artifacts["recovery_worker"] = {
            "recovery_status": state.get("recovery_status", "skipped"),
        }
        result = await factory.create("communication_lead").run(task)

        final_status = task.context.approved_artifacts.get("__final_status__", "monitoring")
        return {
            "final_status": final_status,
            "phase_status": "running" if result.status.value != "failed" else "degraded",
        }

    async def finalize_node(state: IncidentResponseState) -> Dict[str, Any]:
        log.info("incident_response_graph_finalize", incident_id=state["incident_id"])
        if not factory:
            return {"phase_status": "completed"}

        task = _new_task(state, "incident_response_head", "finalize")
        task.context.approved_artifacts["__factory__"] = factory
        task.context.approved_artifacts["__final_status__"] = state.get("final_status", "monitoring")
        result = await factory.create("incident_response_head").run(task)

        return {"phase_status": "completed" if result.status.value != "failed" else "degraded"}

    g = StateGraph(IncidentResponseState)
    g.add_node("intake", intake_node)
    g.add_node("analyze", analyze_node)
    g.add_node("recover", recover_node)
    g.add_node("communicate", communicate_node)
    g.add_node("finalize", finalize_node)

    g.set_entry_point("intake")
    g.add_conditional_edges("intake", route_after_intake, {"analyze": "analyze"})
    g.add_conditional_edges("analyze", route_after_analyze,
                             {"recover": "recover", "communicate": "communicate"})
    g.add_conditional_edges("recover", route_after_recover, {"communicate": "communicate"})
    g.add_conditional_edges("communicate", route_after_communicate, {"finalize": "finalize"})
    g.add_edge("finalize", END)

    kwargs: Dict[str, Any] = {}
    if checkpointer:
        kwargs["checkpointer"] = checkpointer
    return g.compile(**kwargs)


def initial_state(incident_id: str, component: str, severity: str, project_id: str,
                   breach_cycles: int = 1, workflow_id: Optional[str] = None) -> IncidentResponseState:
    return IncidentResponseState(
        incident_id=incident_id, project_id=project_id, workflow_id=workflow_id or str(uuid.uuid4()),
        component=component, severity=severity, breach_cycles=breach_cycles,
        recommended_action=None, recovery_status=None, final_status=None,
        phase_status="pending", failure_reason=None,
    )
