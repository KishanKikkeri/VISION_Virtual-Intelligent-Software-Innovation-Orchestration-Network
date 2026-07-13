"""
services/monitoring/context.py — task decomposition + deterministic
health-score aggregation, alert evaluation, and incident-escalation
logic for M3.7.

Design decision (see docs/M3.7_Monitoring_Service_Specification_v1.md
§1/§2): AGENT_REGISTRY reserves exactly 10 agent_ids for the
`monitoring` department — 1 head, 3 leads (metrics/observability/
alerting), 6 workers. Every deterministic, non-agent step (health-score
composite math, alert dedup, incident-breach counting, capacity
projection) lives here as plain Python, exactly the same
"deterministic X Lead" pattern already used three times before: QA's
Reporting Lead (M3.4), Security's Risk Lead (M3.5), DevOps's Deployment/
Release Lead (M3.6). Monitoring's own Alerting Lead *is* a reserved
agent — but the pure-math portions of severity dedup and breach
counting still don't need an LLM call, so they're implemented here and
invoked by AlertWorker/AlertingLead rather than duplicated per-agent.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from services.monitoring.models import (
    AlertConfiguration,
    AlertEvent,
    AlertRule,
    AlertSeverity,
    CapacityForecast,
    ComponentScore,
    DashboardConfiguration,
    DashboardWidget,
    HealthStatus,
    IncidentCandidate,
    MetricSample,
    MetricsSnapshot,
    MonitoredComponent,
    MonitoringTask,
    MonitoringTaskStatus,
    PerformanceReport,
    SystemHealthReport,
)
from services.monitoring.utils import (
    classify,
    is_deduped,
    mark_alerted,
    project_breach,
    trend_slope,
    weighted_health_score,
)

# Worker assignment per team, matching AGENT_REGISTRY's monitoring-service entries.
METRICS_WORKERS       = ["infrastructure_metrics_worker", "application_metrics_worker"]
OBSERVABILITY_WORKERS = ["log_analysis_worker", "trace_analysis_worker"]
ALERTING_WORKERS      = ["alert_worker", "dashboard_worker"]

_METRICS_DEPS: Dict[str, List[str]] = {
    "infrastructure_metrics_worker": [], "application_metrics_worker": [],
}
_OBSERVABILITY_DEPS: Dict[str, List[str]] = {
    "log_analysis_worker": [], "trace_analysis_worker": [],
}
_ALERTING_DEPS: Dict[str, List[str]] = {
    # dashboard_worker always refreshes; alert_worker only emits on
    # threshold crossing — neither depends on the other (spec §3 step 5).
    "alert_worker": [], "dashboard_worker": [],
}

# Components split by which lead's workers collect them (spec §1/§6).
INFRASTRUCTURE_COMPONENTS = (
    MonitoredComponent.POSTGRES, MonitoredComponent.QDRANT,
    MonitoredComponent.NATS, MonitoredComponent.DOCKER,
)
APPLICATION_COMPONENTS = (
    MonitoredComponent.WEBSOCKET, MonitoredComponent.LLM_PROVIDERS,
    MonitoredComponent.AGENT_RUNTIME, MonitoredComponent.REPOSITORY,
    MonitoredComponent.DEPLOYMENTS,
)


def build_monitoring_tasks() -> List[MonitoringTask]:
    """One cycle's full task list — Collect (metrics+observability) -> Alert/Dashboard."""
    tasks: List[MonitoringTask] = []
    id_by_worker: Dict[str, str] = {}

    def _add(worker_id: str, deps_worker_ids: List[str]) -> None:
        task = MonitoringTask(
            worker_agent_id=worker_id,
            description=f"monitoring cycle step: {worker_id}",
            depends_on=[id_by_worker[d] for d in deps_worker_ids if d in id_by_worker],
        )
        id_by_worker[worker_id] = task.task_id
        tasks.append(task)

    for w in METRICS_WORKERS:
        _add(w, _METRICS_DEPS.get(w, []))
    for w in OBSERVABILITY_WORKERS:
        _add(w, _OBSERVABILITY_DEPS.get(w, []))
    for w in ALERTING_WORKERS:
        _add(w, _ALERTING_DEPS.get(w, []))

    return tasks


def topological_batches(tasks: List[MonitoringTask]) -> List[List[MonitoringTask]]:
    remaining = {t.task_id: t for t in tasks}
    done: Set[str] = set()
    batches: List[List[MonitoringTask]] = []

    while remaining:
        batch = [t for t in remaining.values() if all(d in done for d in t.depends_on)]
        if not batch:
            raise ValueError(f"Dependency cycle detected among Monitoring tasks: {list(remaining.keys())}")
        batches.append(batch)
        for t in batch:
            done.add(t.task_id)
            del remaining[t.task_id]

    return batches


# -- Aggregate (Metrics Lead output -> per-component score) -------------

def aggregate_component_scores(samples: List[MetricSample]) -> Dict[MonitoredComponent, float]:
    """
    Averages sample values per component into a 0-100 score. Providers
    are expected to already emit 0-100 "health" values per §6 (a
    provider that observed a hard failure emits 0 for that component,
    per spec §7's "degrade to 0 rather than retry" rule) — this
    function's only job is grouping/averaging, not judgment.
    """
    grouped: Dict[MonitoredComponent, List[float]] = {}
    for s in samples:
        grouped.setdefault(s.component, []).append(s.value)
    return {
        component: round(sum(values) / len(values), 4)
        for component, values in grouped.items()
        if values
    }


def build_metrics_snapshot(samples: List[MetricSample]) -> MetricsSnapshot:
    return MetricsSnapshot(samples=samples)


def build_health_report(component_scores: Dict[MonitoredComponent, float]) -> SystemHealthReport:
    score = weighted_health_score(component_scores)
    return SystemHealthReport(
        health_score=score,
        status=classify(score),
        component_scores={c.value: v for c, v in component_scores.items()},
    )


# -- Alert evaluation (Alert Worker) ------------------------------------

def evaluate_alerts(
    component_scores: Dict[MonitoredComponent, float],
    last_alert_at: Dict[str, datetime],
    dedup_window_seconds: int,
    now: Optional[datetime] = None,
) -> List[AlertEvent]:
    """
    Emits one AlertEvent per component whose score has crossed below
    WARNING or CRITICAL, unless deduped within the window (spec §5).
    Mutates `last_alert_at` in place for every alert actually emitted.
    """
    events: List[AlertEvent] = []
    for component, score in component_scores.items():
        status = classify(score)
        if status == HealthStatus.HEALTHY:
            continue
        severity = AlertSeverity.CRITICAL if status == HealthStatus.CRITICAL else AlertSeverity.WARNING
        if is_deduped(component.value, severity.value, last_alert_at, dedup_window_seconds, now):
            continue
        events.append(AlertEvent(
            component=component, severity=severity,
            message=f"{component.value} health degraded to {score:.1f} ({status.value})",
        ))
        mark_alerted(component.value, severity.value, last_alert_at, now)
    return events


def default_alert_configuration() -> AlertConfiguration:
    return AlertConfiguration(rules=[
        AlertRule(component=c, severity=AlertSeverity.WARNING, threshold_score=70.0)
        for c in MonitoredComponent
    ])


# -- Dashboard rendering (Dashboard Worker) ------------------------------

def build_dashboard_configuration(component_scores: Dict[MonitoredComponent, float],
                                   health_report: SystemHealthReport) -> DashboardConfiguration:
    widgets = [
        DashboardWidget(widget_type="gauge", title="Platform Health Score",
                         config={"value": health_report.health_score, "status": health_report.status.value},
                         position=0),
    ]
    for i, (component, score) in enumerate(sorted(component_scores.items(), key=lambda kv: kv[0].value), start=1):
        widgets.append(DashboardWidget(
            widget_type="line_chart", title=component.value.replace("_", " ").title(),
            config={"component": component.value, "score": score}, position=i,
        ))
    return DashboardConfiguration(widgets=widgets, layout={"columns": 3})


def render_grafana_export(dashboard: DashboardConfiguration) -> Dict[str, Any]:
    """
    One-way export of the DB-backed dashboard to Grafana provisioning
    JSON (spec §0 Decision 4). Never read back — the DB rows remain
    authoritative.
    """
    return {
        "title": dashboard.name,
        "panels": [
            {"id": i, "title": w.title, "type": w.widget_type, "gridPos": {"x": 0, "y": i, "w": 8, "h": 6}}
            for i, w in enumerate(dashboard.widgets)
        ],
        "schemaVersion": 39,
    }


# -- Incident escalation (Alerting Lead) --------------------------------

def decide_incident(
    component_scores: Dict[MonitoredComponent, float],
    consecutive_critical_count: Dict[str, int],
    breach_cycles_required: int,
) -> List[IncidentCandidate]:
    """
    Increments/resets each component's consecutive-CRITICAL streak and
    returns an IncidentCandidate for every component that has now
    reached `breach_cycles_required` consecutive CRITICAL cycles (spec
    §0 Decision 5). Mutates `consecutive_critical_count` in place —
    callers persist it across cycles via the LangGraph checkpointer
    (spec §8, stable thread_id).
    """
    incidents: List[IncidentCandidate] = []
    for component, score in component_scores.items():
        key = component.value
        if classify(score) == HealthStatus.CRITICAL:
            consecutive_critical_count[key] = consecutive_critical_count.get(key, 0) + 1
        else:
            consecutive_critical_count[key] = 0

        if consecutive_critical_count[key] >= breach_cycles_required:
            incidents.append(IncidentCandidate(
                component=component, severity=AlertSeverity.CRITICAL,
                breach_cycles=consecutive_critical_count[key],
            ))
    return incidents


# -- Capacity forecasting (Metrics Lead) --------------------------------

def build_capacity_forecast(
    component: MonitoredComponent,
    trailing_samples: List[float],
    cycle_interval_seconds: int,
    breach_threshold: float = 70.0,  # WARNING_THRESHOLD, spec §0 Decision 3
) -> CapacityForecast:
    slope = trend_slope(trailing_samples)
    current = trailing_samples[-1] if trailing_samples else 100.0
    breach_at = project_breach(current, slope, breach_threshold, cycle_interval_seconds)
    return CapacityForecast(component=component, trend_slope=slope, projected_breach_at=breach_at)


# -- Performance report (Observability Lead) ----------------------------

def build_performance_report(p95_latency_ms: float, error_rate: float,
                              trace_hotspots: Optional[List[str]] = None) -> PerformanceReport:
    return PerformanceReport(
        p95_latency_ms=p95_latency_ms, error_rate=error_rate,
        trace_hotspots=trace_hotspots or [],
    )


def team_progress(tasks: List[MonitoringTask]) -> Dict[str, int]:
    return {
        "total":     len(tasks),
        "completed": sum(1 for t in tasks if t.status == MonitoringTaskStatus.COMPLETED),
        "failed":    sum(1 for t in tasks if t.status == MonitoringTaskStatus.FAILED),
        "escalated": sum(1 for t in tasks if t.status == MonitoringTaskStatus.ESCALATED),
    }
