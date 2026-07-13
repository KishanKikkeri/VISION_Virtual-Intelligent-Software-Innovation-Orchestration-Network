"""
services/monitoring/workflows/monitoring_graph.py
=======================================================================
W-MONITORING: Monitoring Service LangGraph state machine.

Continuous-execution model (spec §0 Decision 1 / §7): this graph is
BOUNDED — one compiled invocation is exactly one monitoring cycle,
collect_metrics -> score_and_publish -> dashboard_and_alert ->
[incident_handoff] -> publish -> END. "Continuous" is achieved by
`services/monitoring/main.py`'s scheduler re-invoking this compiled
graph on a fixed interval using the SAME `thread_id`, so the Postgres
checkpointer carries `consecutive_critical_count` / `last_alert_at`
forward between cycles — not by any node looping internally.

Deviation from a literal reading of the conceptual 8-step diagram in
docs/M3.7_Monitoring_Service_Specification_v1.md §8: "aggregate",
"analyze", and "health_score" are folded into one `score_and_publish`
node because MetricsLead already aggregates samples into
`component_scores` as part of "collect" (services/monitoring/leads.py
MetricsLead.execute), and MonitoringHead computes the composite score +
capacity forecast + incident decision in one call (services/monitoring/
head/__init__.py). This mirrors DevOpsHead's two-stage split
(services/devops/head/__init__.py) — each node here still corresponds
1:1 to a real agent invocation, unlike services/devops/workflows/
devops_graph.py's nodes, which are pure state-transition markers with
no agent calls (DevOpsHead is invoked directly by Manager, outside its
own graph). Monitoring's graph nodes DO call
`factory.create(agent_id).run(task)` directly, since a continuously
scheduled cycle needs one durable, resumable state machine as its
actual execution engine, not a parallel/optional one.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

import structlog
from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from core.runtime.context import AgentContext, TaskInput
from services.monitoring.routing import (
    route_after_collect,
    route_after_dashboard_and_alert,
    route_after_incident_handoff,
    route_after_score_and_publish,
)

log = structlog.get_logger(__name__)


class MonitoringState(TypedDict):
    cycle_count:                  int
    project_id:                   str     # sentinel platform-anchor project_id, see integration/platform_anchor.py
    workflow_id:                  str

    component_scores:             Dict[str, float]
    health_score:                 float
    status:                       str

    consecutive_critical_count:   Dict[str, int]   # persists across cycles via checkpointer
    last_alert_at:                Dict[str, str]   # persists across cycles via checkpointer (ISO timestamps)
    incident_candidates:          List[Dict[str, Any]]

    cycle_interval_seconds:       int
    incident_breach_cycles:       int
    alert_dedup_seconds:          int

    phase_status:                 str
    failure_reason:               Optional[str]


def _new_task(state: MonitoringState, agent_id: str, task_type: str) -> TaskInput:
    ctx = AgentContext(
        project_id=state["project_id"], workflow_id=state["workflow_id"],
        current_phase=1, project_name="Platform Monitoring",
        project_description="Sentinel context for the continuous Monitoring Service cycle.",
    )
    ctx.approved_artifacts["__component_scores__"] = state.get("component_scores", {})
    ctx.approved_artifacts["__last_alert_at__"] = state.get("last_alert_at", {})
    ctx.approved_artifacts["__dedup_window_seconds__"] = state.get("alert_dedup_seconds", 300)
    ctx.approved_artifacts["__consecutive_critical_count__"] = state.get("consecutive_critical_count", {})
    ctx.approved_artifacts["__incident_breach_cycles__"] = state.get("incident_breach_cycles", 3)

    return TaskInput.create(
        project_id=state["project_id"], agent_id=agent_id, parent_agent_id="monitoring_head",
        task_type=task_type, description=f"Monitoring cycle #{state.get('cycle_count', 0)}: {task_type}",
        expected_output="AgentResult.content per services/monitoring/models.py", context=ctx,
    )


def build_monitoring_graph(factory: Any, checkpointer: Any = None):
    """
    Builds W-MONITORING. `factory` is an AgentFactory instance (or None
    for dry-run/tests, in which case nodes degrade to no-ops — mirrors
    services/devops/leads._run_batches's `if not factory` fallback).
    """

    async def collect_node(state: MonitoringState) -> Dict[str, Any]:
        log.info("monitoring_graph_collect", cycle=state.get("cycle_count", 0))
        if not factory:
            return {"phase_status": "running", "component_scores": {}}

        metrics_task = _new_task(state, "metrics_lead", "collect")
        metrics_task.context.approved_artifacts["__factory__"] = factory
        observability_task = _new_task(state, "observability_lead", "collect")
        observability_task.context.approved_artifacts["__factory__"] = factory

        import asyncio
        metrics_result, _observability_result = await asyncio.gather(
            factory.create("metrics_lead").run(metrics_task),
            factory.create("observability_lead").run(observability_task),
        )

        component_scores = metrics_task.context.approved_artifacts.get("__component_scores__", {})
        return {
            "phase_status": "running" if metrics_result.status.value != "failed" else "degraded",
            "component_scores": component_scores,
        }

    async def score_and_publish_node(state: MonitoringState) -> Dict[str, Any]:
        log.info("monitoring_graph_score", cycle=state.get("cycle_count", 0))
        if not factory:
            return {"health_score": 0.0, "status": "critical", "incident_candidates": []}

        task = _new_task(state, "monitoring_head", "score_and_publish")
        task.context.approved_artifacts["__factory__"] = factory
        result = await factory.create("monitoring_head").run(task)

        content = result.content or {}
        return {
            "health_score": content.get("health_score", 0.0),
            "status": content.get("status", "critical"),
            "incident_candidates": content.get("incidents", []),
            "consecutive_critical_count": content.get("consecutive_critical_count", {}),
            "phase_status": "running" if result.status.value != "failed" else "degraded",
        }

    async def dashboard_and_alert_node(state: MonitoringState) -> Dict[str, Any]:
        log.info("monitoring_graph_dashboard_alert", cycle=state.get("cycle_count", 0))
        if not factory:
            return {"last_alert_at": state.get("last_alert_at", {})}

        task = _new_task(state, "alerting_lead", "dashboard_and_alert")
        task.context.approved_artifacts["__factory__"] = factory
        result = await factory.create("alerting_lead").run(task)

        alert_worker_result = task.context.approved_artifacts.get("alert_worker", {})
        last_alert_at = alert_worker_result.get("last_alert_at", state.get("last_alert_at", {}))
        return {
            "last_alert_at": last_alert_at,
            "phase_status": "running" if result.status.value != "failed" else "degraded",
        }

    async def incident_handoff_node(state: MonitoringState) -> Dict[str, Any]:
        """
        Incident artifacts/events were already published by MonitoringHead
        in score_and_publish_node (spec §0 Decision 5) — this node exists
        so the graph's own shape reflects the conceptual "incident?"
        decision point for observability/testing, and is where M3.8's
        future handoff hook (e.g. a direct notify call) would attach.
        """
        log.warning("monitoring_graph_incident", cycle=state.get("cycle_count", 0),
                    incidents=len(state.get("incident_candidates", [])))
        return {"phase_status": "running"}

    async def publish_node(state: MonitoringState) -> Dict[str, Any]:
        log.info("monitoring_graph_publish", cycle=state.get("cycle_count", 0),
                 health_score=state.get("health_score"))
        return {
            "phase_status": "completed",
            "cycle_count": state.get("cycle_count", 0) + 1,
        }

    g = StateGraph(MonitoringState)
    g.add_node("collect", collect_node)
    g.add_node("score_and_publish", score_and_publish_node)
    g.add_node("dashboard_and_alert", dashboard_and_alert_node)
    g.add_node("incident_handoff", incident_handoff_node)
    g.add_node("publish", publish_node)

    g.set_entry_point("collect")
    g.add_conditional_edges("collect", route_after_collect, {"score_and_publish": "score_and_publish"})
    g.add_conditional_edges("score_and_publish", route_after_score_and_publish,
                             {"dashboard_and_alert": "dashboard_and_alert"})
    g.add_conditional_edges("dashboard_and_alert", route_after_dashboard_and_alert, {
        "incident_handoff": "incident_handoff", "publish": "publish",
    })
    g.add_conditional_edges("incident_handoff", route_after_incident_handoff, {"publish": "publish"})
    g.add_edge("publish", END)

    kwargs: Dict[str, Any] = {}
    if checkpointer:
        kwargs["checkpointer"] = checkpointer
    return g.compile(**kwargs)


def initial_state(project_id: str, workflow_id: Optional[str] = None,
                   cycle_interval_seconds: int = 30, incident_breach_cycles: int = 3,
                   alert_dedup_seconds: int = 300) -> MonitoringState:
    return MonitoringState(
        cycle_count=0, project_id=project_id, workflow_id=workflow_id or str(uuid.uuid4()),
        component_scores={}, health_score=0.0, status="critical",
        consecutive_critical_count={}, last_alert_at={}, incident_candidates=[],
        cycle_interval_seconds=cycle_interval_seconds, incident_breach_cycles=incident_breach_cycles,
        alert_dedup_seconds=alert_dedup_seconds, phase_status="pending", failure_reason=None,
    )
