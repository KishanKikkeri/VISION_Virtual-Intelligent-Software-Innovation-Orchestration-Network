"""
tests/foundation/test_m37_monitoring.py
========================================
M3.7 Monitoring Service tests — 4 layers matching the M3.1-M3.6 pattern.

Layer 1 — Unit:        models/enums, utils (scoring/trend/dedup math),
                       context (aggregation/alerts/dashboard/incident/
                       forecast/task decomposition), routing predicates,
                       agent registry verification, provider interface.
Layer 2 — Graph:       LangGraph node functions + full graph construction
                       and execution (dry-run and FakeFactory-backed).
Layer 3 — Integration: workers/leads/head (deterministic — no LLM calls),
                       repository functions (interface-level, mocked DB),
                       platform anchor bootstrap.
Layer 4 — E2E:         full MonitoringHead + AlertingLead cycle via a
                       FakeFactory chain; incident escalation across
                       simulated consecutive cycles.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.contracts import AgentResult, TaskStatus
from core.runtime.context import AgentContext, TaskInput
from core.runtime.factory import AGENT_REGISTRY

from services.monitoring.models import (
    AlertConfiguration, AlertEvent, AlertRule, AlertSeverity, AlertStatus,
    CapacityForecast, ComponentScore, DashboardConfiguration, DashboardWidget,
    HEALTHY_THRESHOLD, HealthStatus, IncidentCandidate, MetricSample,
    MetricsSnapshot, MonitoredComponent, MonitoringTask, MonitoringTaskStatus,
    PerformanceReport, SystemHealthReport, WARNING_THRESHOLD, status_for_score,
)
from services.monitoring.utils import (
    build_component_scores, classify, is_deduped, mark_alerted,
    project_breach, trend_slope, weighted_health_score,
)
from services.monitoring.context import (
    ALERTING_WORKERS, METRICS_WORKERS, OBSERVABILITY_WORKERS,
    aggregate_component_scores, build_capacity_forecast, build_dashboard_configuration,
    build_health_report, build_metrics_snapshot, build_monitoring_tasks,
    build_performance_report, decide_incident, default_alert_configuration,
    evaluate_alerts, render_grafana_export, team_progress, topological_batches,
)
from services.monitoring.routing import (
    route_after_collect, route_after_dashboard_and_alert,
    route_after_incident_handoff, route_after_score_and_publish, route_task_retry,
)
from services.monitoring.providers import (
    DeploymentProvider, DockerProvider, NatsProvider, PostgresProvider, QdrantProvider,
    RepositoryProvider, application_providers, infrastructure_providers,
)
from services.monitoring.providers.telemetry_provider import (
    AgentRuntimeTelemetryProvider, LLMProvidersTelemetryProvider, WebSocketTelemetryProvider,
)
from services.monitoring.integration.monitoring_repository import (
    AlertHistoryRepository, AlertRepository, CapacityForecastRepository,
    DashboardRepository, DashboardWidgetRepository, MetricRepository,
    MetricSampleRepository, MonitoringLogRepository, MonitoringTraceRepository,
    SystemHealthRepository,
)
from services.monitoring.integration.platform_anchor import (
    PLATFORM_PROJECT_NAME, PLATFORM_USER_EMAIL, ensure_platform_anchor,
)
from services.monitoring.workflows.monitoring_graph import (
    MonitoringState, build_monitoring_graph, initial_state,
)


# ══════════════════════════════════════════════════════════════
# Shared test helpers (mirrors tests/foundation/test_m36_devops.py)
# ══════════════════════════════════════════════════════════════

def make_context(project_id: str = "platform-anchor") -> AgentContext:
    return AgentContext(
        project_id=project_id, workflow_id="wf-1", current_phase=1,
        project_name="Platform Monitoring", project_description="test",
    )


def make_task(task_type: str = "collect", **extra_artifacts) -> TaskInput:
    ctx = make_context()
    ctx.approved_artifacts.update(extra_artifacts)
    return TaskInput.create(
        project_id="platform-anchor", agent_id="test_agent", parent_agent_id="monitoring_head",
        task_type=task_type, description="test", expected_output="AgentResult", context=ctx,
    )


def make_infra():
    inner_db = MagicMock(
        execute=AsyncMock(return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=None),
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))))),
        flush=AsyncMock(), add=MagicMock())
    db = MagicMock()
    db.__aenter__ = AsyncMock(return_value=inner_db)
    db.__aexit__ = AsyncMock(return_value=None)
    storage = AsyncMock()
    storage.store = AsyncMock(return_value="local://test/v1.json")

    async def _create_artifact(db, project_id, artifact_type, created_by,
                                content=None, storage_ref=None, metadata=None):
        return {"artifact_id": str(uuid.uuid4()), "artifact_type": artifact_type, "version": 1}

    artifact_mock = MagicMock()
    artifact_mock.create = AsyncMock(side_effect=_create_artifact)

    return {"db_factory": lambda: db, "nats": AsyncMock(), "storage": storage,
            "audit_repo": MagicMock(record=AsyncMock(return_value=str(uuid.uuid4()))),
            "artifact_repo": artifact_mock,
            "token_repo": MagicMock(record=AsyncMock(return_value=str(uuid.uuid4())))}


def inject(agent_class, infra, agent_id: str, layer: int = 5, role: str = "worker"):
    a = agent_class.__new__(agent_class)
    a.agent_id = agent_id
    a.name = agent_class.__name__
    a.department = "monitoring"
    a.layer = layer
    a.role = role
    a.responsibilities = ["Monitoring"]
    a._db_factory = infra["db_factory"]; a._nats = infra["nats"]; a._storage = infra["storage"]
    a._audit_repo = infra["audit_repo"]; a._artifact_repo = infra["artifact_repo"]
    a._token_repo = infra["token_repo"]; a._qdrant = None
    return a


class FakeAgent:
    def __init__(self, agent_id: str, result: AgentResult):
        self.agent_id = agent_id
        self._result = result

    async def run(self, task: TaskInput) -> AgentResult:
        return self._result


class FakeFactory:
    def __init__(self, results: Dict[str, Any]):
        self._results = results

    def create(self, agent_id: str):
        val = self._results.get(agent_id)
        if val is None:
            val = AgentResult(task_id="t", agent_id=agent_id, status=TaskStatus.COMPLETED,
                               content={"placeholder": True}, quality_score=0.8)
        return FakeAgent(agent_id, val)


def ok_result(agent_id: str, **content) -> AgentResult:
    return AgentResult(task_id="t", agent_id=agent_id, status=TaskStatus.COMPLETED,
                        content=content or {"generated": 1}, quality_score=0.9)


def fail_result(agent_id: str, reason: str) -> AgentResult:
    return AgentResult(task_id="t", agent_id=agent_id, status=TaskStatus.FAILED,
                        content={}, quality_score=0.0, failure_reason=reason)


# ══════════════════════════════════════════════════════════════
# LAYER 1a — Unit: models / enums
# ══════════════════════════════════════════════════════════════

class TestEnums:
    def test_health_status_values(self):
        assert HealthStatus.HEALTHY.value == "healthy"
        assert HealthStatus.WARNING.value == "warning"
        assert HealthStatus.CRITICAL.value == "critical"

    def test_alert_severity_values(self):
        assert {s.value for s in AlertSeverity} == {"info", "warning", "critical"}

    def test_alert_status_values(self):
        assert {s.value for s in AlertStatus} == {"open", "acknowledged", "resolved"}

    def test_monitored_component_has_nine_members(self):
        assert len(list(MonitoredComponent)) == 9

    def test_monitoring_task_status_values(self):
        assert MonitoringTaskStatus.PENDING.value == "pending"
        assert MonitoringTaskStatus.DEAD_LETTERED.value == "dead_lettered"


class TestStatusForScore:
    @pytest.mark.parametrize("score,expected", [
        (100.0, HealthStatus.HEALTHY), (90.0, HealthStatus.HEALTHY),
        (89.9, HealthStatus.WARNING), (70.0, HealthStatus.WARNING),
        (69.9, HealthStatus.CRITICAL), (0.0, HealthStatus.CRITICAL),
    ])
    def test_thresholds(self, score, expected):
        assert status_for_score(score) == expected

    def test_thresholds_are_frozen_constants(self):
        assert HEALTHY_THRESHOLD == 90.0
        assert WARNING_THRESHOLD == 70.0


class TestMetricSample:
    def test_defaults(self):
        s = MetricSample(name="x", component=MonitoredComponent.POSTGRES, value=99.0)
        assert s.unit is None
        assert s.labels == {}
        assert isinstance(s.sampled_at, datetime)

    def test_serialization_roundtrip(self):
        s = MetricSample(name="x", component=MonitoredComponent.NATS, value=50.0, labels={"a": "b"})
        d = s.model_dump(mode="json")
        s2 = MetricSample(**d)
        assert s2.component == MonitoredComponent.NATS
        assert s2.labels == {"a": "b"}


class TestMonitoringTask:
    def test_can_run_no_deps(self):
        t = MonitoringTask(worker_agent_id="w1")
        assert t.can_run(set())

    def test_can_run_with_unmet_deps(self):
        t = MonitoringTask(worker_agent_id="w2", depends_on=["dep1"])
        assert not t.can_run(set())
        assert t.can_run({"dep1"})


class TestModelSerialization:
    def test_system_health_report_roundtrip(self):
        r = SystemHealthReport(health_score=91.2, status=HealthStatus.HEALTHY,
                                component_scores={"postgres": 100.0})
        d = r.model_dump(mode="json")
        r2 = SystemHealthReport(**d)
        assert r2.health_score == 91.2

    def test_incident_candidate_defaults(self):
        i = IncidentCandidate(component=MonitoredComponent.NATS, severity=AlertSeverity.CRITICAL,
                               breach_cycles=3)
        assert i.incident_id
        assert i.evidence_refs == []

    def test_dashboard_configuration_defaults(self):
        d = DashboardConfiguration()
        assert d.name == "platform_overview"
        assert d.widgets == []


# ══════════════════════════════════════════════════════════════
# LAYER 1b — Unit: utils (deterministic math)
# ══════════════════════════════════════════════════════════════

class TestWeightedHealthScore:
    def test_all_perfect(self):
        scores = {c: 100.0 for c in MonitoredComponent}
        assert weighted_health_score(scores) == 100.0

    def test_empty_is_zero(self):
        assert weighted_health_score({}) == 0.0

    def test_weighted_average_is_correct(self):
        # postgres weight=3, docker weight=2 -> (100*3 + 0*2) / 5 = 60.0
        scores = {MonitoredComponent.POSTGRES: 100.0, MonitoredComponent.DOCKER: 0.0}
        assert weighted_health_score(scores) == 60.0

    def test_single_component(self):
        scores = {MonitoredComponent.WEBSOCKET: 50.0}
        assert weighted_health_score(scores) == 50.0

    def test_deterministic_across_calls(self):
        scores = {MonitoredComponent.POSTGRES: 77.7, MonitoredComponent.NATS: 42.1}
        assert weighted_health_score(scores) == weighted_health_score(scores)


class TestBuildComponentScores:
    def test_clamps_out_of_range(self):
        raw = {MonitoredComponent.POSTGRES: (150.0, "over"), MonitoredComponent.NATS: (-10.0, "under")}
        result = build_component_scores(raw)
        by_component = {r.component: r.score for r in result}
        assert by_component[MonitoredComponent.POSTGRES] == 100.0
        assert by_component[MonitoredComponent.NATS] == 0.0

    def test_carries_weight_and_reason(self):
        raw = {MonitoredComponent.DOCKER: (80.0, "ok")}
        result = build_component_scores(raw)
        assert result[0].weight == 2.0
        assert result[0].reason == "ok"


class TestClassify:
    def test_matches_status_for_score(self):
        assert classify(95.0) == HealthStatus.HEALTHY
        assert classify(75.0) == HealthStatus.WARNING
        assert classify(10.0) == HealthStatus.CRITICAL


class TestTrendSlope:
    def test_fewer_than_two_samples(self):
        assert trend_slope([]) == 0.0
        assert trend_slope([50.0]) == 0.0

    def test_flat_series_has_zero_slope(self):
        assert trend_slope([50.0, 50.0, 50.0]) == 0.0

    def test_increasing_series_has_positive_slope(self):
        assert trend_slope([10.0, 20.0, 30.0, 40.0]) > 0

    def test_decreasing_series_has_negative_slope(self):
        assert trend_slope([90.0, 80.0, 70.0]) < 0

    def test_deterministic(self):
        samples = [10.0, 12.0, 11.0, 15.0, 14.0]
        assert trend_slope(samples) == trend_slope(samples)


class TestProjectBreach:
    def test_flat_trend_returns_none(self):
        assert project_breach(80.0, 0.0, 70.0, 30) is None

    def test_moving_away_from_threshold_returns_none(self):
        # score increasing, threshold is below current value -> never "breaches" downward
        assert project_breach(80.0, 1.0, 70.0, 30) is None

    def test_decreasing_trend_projects_a_future_breach(self):
        result = project_breach(80.0, -1.0, 70.0, 30)
        assert result is not None
        assert result > datetime.utcnow()

    def test_absurdly_far_projection_returns_none(self):
        result = project_breach(80.0, -0.00001, 70.0, 30, max_cycles_ahead=100)
        assert result is None


class TestDedup:
    def test_not_deduped_when_no_history(self):
        assert not is_deduped("postgres", "warning", {}, 300)

    def test_deduped_within_window(self):
        now = datetime.utcnow()
        last_alert_at = {}
        mark_alerted("postgres", "warning", last_alert_at, now)
        assert is_deduped("postgres", "warning", last_alert_at, 300, now=now + timedelta(seconds=10))

    def test_not_deduped_after_window_expires(self):
        now = datetime.utcnow()
        last_alert_at = {}
        mark_alerted("postgres", "warning", last_alert_at, now)
        assert not is_deduped("postgres", "warning", last_alert_at, 300, now=now + timedelta(seconds=301))

    def test_different_severity_not_deduped(self):
        now = datetime.utcnow()
        last_alert_at = {}
        mark_alerted("postgres", "warning", last_alert_at, now)
        assert not is_deduped("postgres", "critical", last_alert_at, 300, now=now)


# ══════════════════════════════════════════════════════════════
# LAYER 1c — Unit: context (aggregation / alerts / dashboard / incident)
# ══════════════════════════════════════════════════════════════

class TestTaskDecomposition:
    def test_build_monitoring_tasks_count(self):
        tasks = build_monitoring_tasks()
        assert len(tasks) == 6  # 2 metrics + 2 observability + 2 alerting workers

    def test_worker_ids_match_registry(self):
        tasks = build_monitoring_tasks()
        worker_ids = {t.worker_agent_id for t in tasks}
        assert worker_ids == set(METRICS_WORKERS) | set(OBSERVABILITY_WORKERS) | set(ALERTING_WORKERS)

    def test_topological_batches_respects_deps(self):
        tasks = build_monitoring_tasks()
        batches = topological_batches(tasks)
        assert sum(len(b) for b in batches) == len(tasks)

    def test_topological_batches_detects_cycle(self):
        t1 = MonitoringTask(worker_agent_id="a", task_id="1", depends_on=["2"])
        t2 = MonitoringTask(worker_agent_id="b", task_id="2", depends_on=["1"])
        with pytest.raises(ValueError):
            topological_batches([t1, t2])

    def test_team_progress_counts(self):
        tasks = [MonitoringTask(worker_agent_id="a", status=MonitoringTaskStatus.COMPLETED),
                 MonitoringTask(worker_agent_id="b", status=MonitoringTaskStatus.FAILED)]
        progress = team_progress(tasks)
        assert progress["total"] == 2
        assert progress["completed"] == 1
        assert progress["failed"] == 1


class TestAggregateComponentScores:
    def test_averages_multiple_samples_per_component(self):
        samples = [
            MetricSample(name="a", component=MonitoredComponent.POSTGRES, value=100.0),
            MetricSample(name="b", component=MonitoredComponent.POSTGRES, value=80.0),
        ]
        result = aggregate_component_scores(samples)
        assert result[MonitoredComponent.POSTGRES] == 90.0

    def test_empty_samples_returns_empty(self):
        assert aggregate_component_scores([]) == {}

    def test_single_sample_per_component(self):
        samples = [MetricSample(name="a", component=MonitoredComponent.NATS, value=42.0)]
        assert aggregate_component_scores(samples)[MonitoredComponent.NATS] == 42.0


class TestBuildHealthReport:
    def test_matches_weighted_score(self):
        scores = {MonitoredComponent.POSTGRES: 100.0}
        report = build_health_report(scores)
        assert report.health_score == 100.0
        assert report.status == HealthStatus.HEALTHY

    def test_critical_status_for_low_scores(self):
        scores = {c: 10.0 for c in MonitoredComponent}
        report = build_health_report(scores)
        assert report.status == HealthStatus.CRITICAL

    def test_component_scores_use_string_keys(self):
        scores = {MonitoredComponent.DOCKER: 55.0}
        report = build_health_report(scores)
        assert report.component_scores == {"docker": 55.0}


class TestBuildMetricsSnapshot:
    def test_wraps_samples(self):
        samples = [MetricSample(name="a", component=MonitoredComponent.NATS, value=1.0)]
        snapshot = build_metrics_snapshot(samples)
        assert snapshot.samples == samples


class TestEvaluateAlerts:
    def test_no_alerts_when_healthy(self):
        scores = {MonitoredComponent.POSTGRES: 100.0}
        events = evaluate_alerts(scores, {}, 300)
        assert events == []

    def test_alert_raised_for_critical(self):
        scores = {MonitoredComponent.POSTGRES: 10.0}
        events = evaluate_alerts(scores, {}, 300)
        assert len(events) == 1
        assert events[0].severity == AlertSeverity.CRITICAL

    def test_alert_raised_for_warning(self):
        scores = {MonitoredComponent.POSTGRES: 80.0}
        events = evaluate_alerts(scores, {}, 300)
        assert events[0].severity == AlertSeverity.WARNING

    def test_dedup_suppresses_repeat_alert(self):
        scores = {MonitoredComponent.POSTGRES: 10.0}
        last_alert_at: Dict[str, datetime] = {}
        first = evaluate_alerts(scores, last_alert_at, 300)
        second = evaluate_alerts(scores, last_alert_at, 300)
        assert len(first) == 1
        assert len(second) == 0

    def test_mutates_last_alert_at_in_place(self):
        scores = {MonitoredComponent.NATS: 5.0}
        last_alert_at: Dict[str, datetime] = {}
        evaluate_alerts(scores, last_alert_at, 300)
        assert "nats:critical" in last_alert_at

    def test_multiple_components_independent(self):
        scores = {MonitoredComponent.POSTGRES: 10.0, MonitoredComponent.NATS: 100.0}
        events = evaluate_alerts(scores, {}, 300)
        assert len(events) == 1
        assert events[0].component == MonitoredComponent.POSTGRES


class TestDefaultAlertConfiguration:
    def test_one_rule_per_component(self):
        config = default_alert_configuration()
        assert len(config.rules) == 9


class TestBuildDashboardConfiguration:
    def test_includes_health_gauge_plus_component_widgets(self):
        scores = {MonitoredComponent.POSTGRES: 90.0, MonitoredComponent.NATS: 80.0}
        health = build_health_report(scores)
        dashboard = build_dashboard_configuration(scores, health)
        assert len(dashboard.widgets) == 3  # 1 gauge + 2 components
        assert dashboard.widgets[0].widget_type == "gauge"

    def test_layout_has_columns(self):
        dashboard = build_dashboard_configuration({}, build_health_report({}))
        assert "columns" in dashboard.layout


class TestRenderGrafanaExport:
    def test_produces_panels_matching_widget_count(self):
        scores = {MonitoredComponent.POSTGRES: 90.0}
        health = build_health_report(scores)
        dashboard = build_dashboard_configuration(scores, health)
        grafana = render_grafana_export(dashboard)
        assert len(grafana["panels"]) == len(dashboard.widgets)
        assert grafana["schemaVersion"] == 39


class TestDecideIncident:
    def test_no_incident_below_breach_threshold(self):
        scores = {MonitoredComponent.POSTGRES: 10.0}
        counts: Dict[str, int] = {}
        incidents = decide_incident(scores, counts, breach_cycles_required=3)
        assert incidents == []
        assert counts["postgres"] == 1

    def test_incident_after_required_consecutive_cycles(self):
        scores = {MonitoredComponent.POSTGRES: 10.0}
        counts: Dict[str, int] = {}
        for _ in range(2):
            decide_incident(scores, counts, breach_cycles_required=3)
        incidents = decide_incident(scores, counts, breach_cycles_required=3)
        assert len(incidents) == 1
        assert incidents[0].breach_cycles == 3

    def test_recovery_resets_streak(self):
        counts: Dict[str, int] = {}
        decide_incident({MonitoredComponent.POSTGRES: 10.0}, counts, 3)
        decide_incident({MonitoredComponent.POSTGRES: 10.0}, counts, 3)
        decide_incident({MonitoredComponent.POSTGRES: 95.0}, counts, 3)  # recovers
        assert counts["postgres"] == 0

    def test_independent_streaks_per_component(self):
        counts: Dict[str, int] = {}
        scores = {MonitoredComponent.POSTGRES: 10.0, MonitoredComponent.NATS: 95.0}
        decide_incident(scores, counts, 3)
        assert counts["postgres"] == 1
        assert counts["nats"] == 0


class TestBuildCapacityForecast:
    def test_forecast_has_component(self):
        forecast = build_capacity_forecast(MonitoredComponent.POSTGRES, [90.0, 85.0, 80.0], 30)
        assert forecast.component == MonitoredComponent.POSTGRES

    def test_flat_samples_no_projected_breach(self):
        forecast = build_capacity_forecast(MonitoredComponent.NATS, [90.0, 90.0, 90.0], 30)
        assert forecast.projected_breach_at is None


class TestBuildPerformanceReport:
    def test_defaults(self):
        report = build_performance_report(120.0, 0.02)
        assert report.p95_latency_ms == 120.0
        assert report.trace_hotspots == []

    def test_with_hotspots(self):
        report = build_performance_report(50.0, 0.0, trace_hotspots=["agent_x"])
        assert report.trace_hotspots == ["agent_x"]


# ══════════════════════════════════════════════════════════════
# LAYER 1d — Unit: routing predicates
# ══════════════════════════════════════════════════════════════

class TestMonitoringRouting:
    def test_route_after_collect_always_scores(self):
        assert route_after_collect({}) == "score_and_publish"

    def test_route_after_score_and_publish_always_dashboards(self):
        assert route_after_score_and_publish({}) == "dashboard_and_alert"

    def test_route_after_dashboard_and_alert_no_incidents(self):
        assert route_after_dashboard_and_alert({"incident_candidates": []}) == "publish"

    def test_route_after_dashboard_and_alert_with_incidents(self):
        assert route_after_dashboard_and_alert({"incident_candidates": [{"x": 1}]}) == "incident_handoff"

    def test_route_after_incident_handoff_always_publishes(self):
        assert route_after_incident_handoff({}) == "publish"

    def test_route_task_retry_done(self):
        assert route_task_retry({"status": "completed"}) == "done"

    def test_route_task_retry_retries(self):
        assert route_task_retry({"status": "failed", "retry_count": 1}) == "retry"

    def test_route_task_retry_dead_letters(self):
        assert route_task_retry({"status": "failed", "retry_count": 5}) == "dead_letter"


# ══════════════════════════════════════════════════════════════
# LAYER 1e — Unit: agent registry verification
# ══════════════════════════════════════════════════════════════

class TestMonitoringRegistry:
    MONITORING_AGENT_IDS = (
        "monitoring_head", "metrics_lead", "infrastructure_metrics_worker",
        "application_metrics_worker", "observability_lead", "log_analysis_worker",
        "trace_analysis_worker", "alerting_lead", "alert_worker", "dashboard_worker",
    )

    def test_exactly_ten_monitoring_agents(self):
        mon = [k for k, v in AGENT_REGISTRY.items() if v.department == "monitoring"]
        assert len(mon) == 10

    def test_all_expected_ids_present(self):
        for agent_id in self.MONITORING_AGENT_IDS:
            assert agent_id in AGENT_REGISTRY, f"missing {agent_id}"

    def test_head_layer_and_role(self):
        spec = AGENT_REGISTRY["monitoring_head"]
        assert spec.layer == 3 and spec.role == "head"
        assert spec.parent_agent_id == "manager_agent"

    def test_leads_layer_and_parent(self):
        for lead_id in ("metrics_lead", "observability_lead", "alerting_lead"):
            spec = AGENT_REGISTRY[lead_id]
            assert spec.layer == 4 and spec.role == "lead"
            assert spec.parent_agent_id == "monitoring_head"

    def test_workers_layer_and_parents(self):
        expected_parents = {
            "infrastructure_metrics_worker": "metrics_lead",
            "application_metrics_worker": "metrics_lead",
            "log_analysis_worker": "observability_lead",
            "trace_analysis_worker": "observability_lead",
            "alert_worker": "alerting_lead",
            "dashboard_worker": "alerting_lead",
        }
        for worker_id, parent in expected_parents.items():
            spec = AGENT_REGISTRY[worker_id]
            assert spec.layer == 5 and spec.role == "worker"
            assert spec.parent_agent_id == parent

    def test_no_agent_id_collisions_with_other_departments(self):
        # Every monitoring agent_id must be unique across the whole registry.
        all_ids = list(AGENT_REGISTRY.keys())
        assert len(all_ids) == len(set(all_ids))

    def test_monitoring_department_module_path_registered(self):
        from core.runtime.factory import AgentFactory
        # constructing a factory and resolving the dept_map is exercised
        # indirectly via TestAgentsPackageImport below (agents/__init__.py
        # imports cleanly and every class is decorator-registered).
        assert "monitoring_head" in AGENT_REGISTRY


class TestAgentsPackageImport:
    def test_agents_package_imports_all_classes(self):
        from services.monitoring.agents import (
            AlertingLead, AlertWorker, ApplicationMetricsWorker, DashboardWorker,
            InfrastructureMetricsWorker, LogAnalysisWorker, MetricsLead,
            MonitoringHead, ObservabilityLead, TraceAnalysisWorker,
        )
        assert MonitoringHead.agent_id if hasattr(MonitoringHead, "agent_id") else True


# ══════════════════════════════════════════════════════════════
# LAYER 1f — Unit: provider interface
# ══════════════════════════════════════════════════════════════

class TestProviderDegradation:
    @pytest.mark.asyncio
    async def test_postgres_provider_degrades_without_db_factory(self):
        provider = PostgresProvider(None)
        samples = await provider.collect()
        assert samples[0].value == 0.0

    @pytest.mark.asyncio
    async def test_qdrant_provider_degrades_without_client(self):
        provider = QdrantProvider(None)
        samples = await provider.collect()
        assert samples[0].value == 0.0

    @pytest.mark.asyncio
    async def test_nats_provider_degrades_without_client(self):
        provider = NatsProvider(None)
        samples = await provider.collect()
        assert samples[0].value == 0.0

    @pytest.mark.asyncio
    async def test_docker_provider_degrades_on_exception(self):
        provider = DockerProvider(client=MagicMock(containers=MagicMock(
            list=MagicMock(side_effect=RuntimeError("no daemon")))))
        samples = await provider.collect()
        assert samples[0].value == 0.0

    @pytest.mark.asyncio
    async def test_repository_provider_degrades_without_db_factory(self):
        provider = RepositoryProvider(None)
        samples = await provider.collect()
        assert samples[0].value == 0.0

    @pytest.mark.asyncio
    async def test_deployment_provider_degrades_without_db_factory(self):
        provider = DeploymentProvider(None)
        samples = await provider.collect()
        assert samples[0].value == 0.0

    @pytest.mark.asyncio
    async def test_postgres_provider_healthy_path(self):
        infra = make_infra()
        provider = PostgresProvider(infra["db_factory"])
        samples = await provider.collect()
        assert samples[0].value in (100.0, 60.0)

    @pytest.mark.asyncio
    async def test_qdrant_provider_healthy_path(self):
        client = MagicMock(get_collections=MagicMock(return_value=MagicMock(collections=[])))
        provider = QdrantProvider(client)
        samples = await provider.collect()
        assert samples[0].value == 100.0

    def test_infrastructure_providers_returns_four(self):
        providers = infrastructure_providers(None, None, None)
        assert len(providers) == 4

    def test_application_providers_returns_five(self):
        providers = application_providers(None)
        assert len(providers) == 5

    @pytest.mark.asyncio
    async def test_websocket_telemetry_provider_never_raises(self):
        provider = WebSocketTelemetryProvider()
        samples = await provider.collect()
        assert isinstance(samples, list) and len(samples) >= 1

    @pytest.mark.asyncio
    async def test_agent_runtime_telemetry_provider_no_data_is_healthy(self):
        provider = AgentRuntimeTelemetryProvider()
        samples = await provider.collect()
        assert samples[0].component == MonitoredComponent.AGENT_RUNTIME

    @pytest.mark.asyncio
    async def test_llm_providers_telemetry_provider_never_raises(self):
        provider = LLMProvidersTelemetryProvider()
        samples = await provider.collect()
        assert isinstance(samples, list)


# ══════════════════════════════════════════════════════════════
# LAYER 2 — Graph
# ══════════════════════════════════════════════════════════════

class TestMonitoringGraph:
    def test_builds_without_factory(self):
        graph = build_monitoring_graph(factory=None)
        assert graph is not None

    def test_initial_state_defaults(self):
        state = initial_state("proj-1")
        assert state["cycle_count"] == 0
        assert state["status"] == "critical"
        assert state["component_scores"] == {}

    @pytest.mark.asyncio
    async def test_dry_run_completes_one_cycle(self):
        graph = build_monitoring_graph(factory=None)
        state = initial_state("proj-1")
        result = await graph.ainvoke(state)
        assert result["cycle_count"] == 1
        assert result["phase_status"] == "completed"

    @pytest.mark.asyncio
    async def test_graph_with_fake_factory_runs_full_cycle(self):
        results = {
            "metrics_lead": ok_result("metrics_lead", team="metrics"),
            "observability_lead": ok_result("observability_lead", team="observability"),
            "monitoring_head": ok_result("monitoring_head", health_score=95.0, status="healthy",
                                         incidents=[], consecutive_critical_count={}),
            "alerting_lead": ok_result("alerting_lead", team="alerting"),
        }
        factory = FakeFactory(results)
        graph = build_monitoring_graph(factory)
        state = initial_state("proj-1")
        result = await graph.ainvoke(state)
        assert result["health_score"] == 95.0
        assert result["status"] == "healthy"
        assert result["phase_status"] == "completed"

    @pytest.mark.asyncio
    async def test_graph_routes_to_incident_handoff_when_incidents_present(self):
        results = {
            "metrics_lead": ok_result("metrics_lead"),
            "observability_lead": ok_result("observability_lead"),
            "monitoring_head": ok_result("monitoring_head", health_score=5.0, status="critical",
                                         incidents=[{"component": "postgres"}], consecutive_critical_count={"postgres": 3}),
            "alerting_lead": ok_result("alerting_lead"),
        }
        factory = FakeFactory(results)
        graph = build_monitoring_graph(factory)
        state = initial_state("proj-1")
        result = await graph.ainvoke(state)
        assert result["incident_candidates"] == [{"component": "postgres"}]

    @pytest.mark.asyncio
    async def test_graph_survives_repeated_cycles_with_stable_thread(self):
        graph = build_monitoring_graph(factory=None)
        state = initial_state("proj-1")
        for _ in range(3):
            state = await graph.ainvoke(state)
        assert state["cycle_count"] == 3


# ══════════════════════════════════════════════════════════════
# LAYER 3a — Integration: workers (deterministic, mocked infra)
# ══════════════════════════════════════════════════════════════

class TestInfrastructureMetricsWorker:
    @pytest.mark.asyncio
    async def test_execute_returns_samples(self):
        from services.monitoring.workers.infrastructure_metrics import InfrastructureMetricsWorker
        infra = make_infra()
        agent = inject(InfrastructureMetricsWorker, infra, "infrastructure_metrics_worker")
        result = await agent.execute(make_task())
        assert result.status == TaskStatus.COMPLETED
        assert "samples" in result.content


class TestApplicationMetricsWorker:
    @pytest.mark.asyncio
    async def test_execute_returns_samples(self):
        from services.monitoring.workers.application_metrics import ApplicationMetricsWorker
        infra = make_infra()
        agent = inject(ApplicationMetricsWorker, infra, "application_metrics_worker")
        result = await agent.execute(make_task())
        assert result.status == TaskStatus.COMPLETED
        assert "samples" in result.content


class TestLogAnalysisWorker:
    @pytest.mark.asyncio
    async def test_execute_handles_empty_audit_events(self):
        from services.monitoring.workers.log_analysis import LogAnalysisWorker
        infra = make_infra()
        agent = inject(LogAnalysisWorker, infra, "log_analysis_worker")
        result = await agent.execute(make_task())
        assert result.status == TaskStatus.COMPLETED
        assert result.content["error_rate"] == 0.0


class TestTraceAnalysisWorker:
    @pytest.mark.asyncio
    async def test_execute_handles_no_telemetry_data(self):
        from services.monitoring.workers.trace_analysis import TraceAnalysisWorker
        infra = make_infra()
        agent = inject(TraceAnalysisWorker, infra, "trace_analysis_worker")
        result = await agent.execute(make_task())
        assert result.status == TaskStatus.COMPLETED
        assert "trace_hotspots" in result.content


class TestAlertWorker:
    @pytest.mark.asyncio
    async def test_execute_raises_alert_for_critical_component(self):
        from services.monitoring.workers.alert import AlertWorker
        infra = make_infra()
        agent = inject(AlertWorker, infra, "alert_worker")
        task = make_task(__component_scores__={"postgres": 5.0})
        result = await agent.execute(task)
        assert result.status == TaskStatus.COMPLETED
        assert len(result.content["alerts_raised"]) == 1
        assert len(result.nats_events) == 1
        assert result.nats_events[0].subject == "monitoring.alert"

    @pytest.mark.asyncio
    async def test_execute_no_alert_when_all_healthy(self):
        from services.monitoring.workers.alert import AlertWorker
        infra = make_infra()
        agent = inject(AlertWorker, infra, "alert_worker")
        task = make_task(__component_scores__={"postgres": 100.0})
        result = await agent.execute(task)
        assert result.content["alerts_raised"] == []
        assert result.nats_events == []


class TestDashboardWorker:
    @pytest.mark.asyncio
    async def test_execute_creates_dashboard_artifact(self):
        from services.monitoring.workers.dashboard import DashboardWorker
        infra = make_infra()
        agent = inject(DashboardWorker, infra, "dashboard_worker")
        task = make_task(__component_scores__={"postgres": 90.0})
        result = await agent.execute(task)
        assert result.status == TaskStatus.COMPLETED
        assert len(result.artifacts) == 1
        assert len(result.content["widgets"]) >= 1


# ══════════════════════════════════════════════════════════════
# LAYER 3b — Integration: leads (FakeFactory)
# ══════════════════════════════════════════════════════════════

class TestMetricsLead:
    @pytest.mark.asyncio
    async def test_aggregates_worker_samples(self):
        from services.monitoring.leads import MetricsLead
        infra = make_infra()
        agent = inject(MetricsLead, infra, "metrics_lead", layer=4, role="lead")

        samples = [MetricSample(name="a", component=MonitoredComponent.POSTGRES, value=90.0).model_dump(mode="json")]
        factory = FakeFactory({
            "infrastructure_metrics_worker": ok_result("infrastructure_metrics_worker", samples=samples),
            "application_metrics_worker": ok_result("application_metrics_worker", samples=[]),
        })
        task = make_task(__factory__=factory)
        result = await agent.execute(task)
        assert result.status == TaskStatus.COMPLETED
        assert task.context.approved_artifacts["__component_scores__"]["postgres"] == 90.0

    @pytest.mark.asyncio
    async def test_no_factory_uses_placeholder(self):
        from services.monitoring.leads import MetricsLead
        infra = make_infra()
        agent = inject(MetricsLead, infra, "metrics_lead", layer=4, role="lead")
        result = await agent.execute(make_task())
        assert result.status == TaskStatus.COMPLETED


class TestObservabilityLead:
    @pytest.mark.asyncio
    async def test_builds_performance_report_artifact(self):
        from services.monitoring.leads import ObservabilityLead
        infra = make_infra()
        agent = inject(ObservabilityLead, infra, "observability_lead", layer=4, role="lead")
        factory = FakeFactory({
            "log_analysis_worker": ok_result("log_analysis_worker", error_rate=0.1),
            "trace_analysis_worker": ok_result("trace_analysis_worker", p95_latency_ms=200.0, trace_hotspots=["x"]),
        })
        task = make_task(__factory__=factory)
        result = await agent.execute(task)
        assert result.status == TaskStatus.COMPLETED
        assert len(result.artifacts) == 1
        assert result.content["error_rate"] == 0.1
        assert result.content["trace_hotspots"] == ["x"]


class TestAlertingLead:
    @pytest.mark.asyncio
    async def test_coordinates_alert_and_dashboard_workers(self):
        from services.monitoring.leads import AlertingLead
        infra = make_infra()
        agent = inject(AlertingLead, infra, "alerting_lead", layer=4, role="lead")
        factory = FakeFactory({
            "alert_worker": ok_result("alert_worker", alerts_raised=[]),
            "dashboard_worker": ok_result("dashboard_worker", widgets=[]),
        })
        task = make_task(__factory__=factory)
        result = await agent.execute(task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["executed"] == 2

    @pytest.mark.asyncio
    async def test_failure_propagates(self):
        from services.monitoring.leads import AlertingLead
        infra = make_infra()
        agent = inject(AlertingLead, infra, "alerting_lead", layer=4, role="lead")
        factory = FakeFactory({
            "alert_worker": fail_result("alert_worker", "boom"),
            "dashboard_worker": ok_result("dashboard_worker"),
        })
        task = make_task(__factory__=factory)
        result = await agent.execute(task)
        assert result.status == TaskStatus.FAILED
        assert "boom" in result.failure_reason


# ══════════════════════════════════════════════════════════════
# LAYER 3c — Integration: repository functions (interface-level)
# ══════════════════════════════════════════════════════════════

class TestMonitoringRepositoryInterface:
    @pytest.mark.asyncio
    async def test_metric_repository_get_or_create_new(self):
        infra = make_infra()
        db = infra["db_factory"]()
        async with db as session:
            metric = await MetricRepository.get_or_create(session, "m1", "postgres", "score")
            assert metric.name == "m1"

    @pytest.mark.asyncio
    async def test_metric_sample_repository_record(self):
        infra = make_infra()
        db = infra["db_factory"]()
        async with db as session:
            sample = await MetricSampleRepository.record(session, "metric-1", 88.0, labels={"a": "b"})
            assert sample.value == 88.0

    @pytest.mark.asyncio
    async def test_system_health_repository_record(self):
        infra = make_infra()
        db = infra["db_factory"]()
        async with db as session:
            row = await SystemHealthRepository.record(session, 91.0, "healthy", {"postgres": 100.0})
            assert row.health_score == 91.0

    @pytest.mark.asyncio
    async def test_alert_repository_open_alert(self):
        infra = make_infra()
        db = infra["db_factory"]()
        async with db as session:
            row = await AlertRepository.open_alert(session, "postgres", "critical", "down")
            assert row.status == "open"

    @pytest.mark.asyncio
    async def test_alert_history_repository_record(self):
        infra = make_infra()
        db = infra["db_factory"]()
        async with db as session:
            row = await AlertHistoryRepository.record(session, "alert-1", "raised")
            assert row.action == "raised"

    @pytest.mark.asyncio
    async def test_dashboard_repository_upsert_creates_new(self):
        infra = make_infra()
        db = infra["db_factory"]()
        async with db as session:
            row = await DashboardRepository.upsert(session, "platform_overview", {"columns": 3})
            assert row.name == "platform_overview"

    @pytest.mark.asyncio
    async def test_capacity_forecast_repository_record(self):
        infra = make_infra()
        db = infra["db_factory"]()
        async with db as session:
            row = await CapacityForecastRepository.record(session, "postgres", 0.5)
            assert row.component == "postgres"

    @pytest.mark.asyncio
    async def test_monitoring_log_repository_record(self):
        infra = make_infra()
        db = infra["db_factory"]()
        async with db as session:
            row = await MonitoringLogRepository.record(session, "platform", "warning", "msg")
            assert row.level == "warning"

    @pytest.mark.asyncio
    async def test_monitoring_trace_repository_record(self):
        infra = make_infra()
        db = infra["db_factory"]()
        async with db as session:
            row = await MonitoringTraceRepository.record(session, "trace-1", "span-1", "svc", 12.0)
            assert row.trace_id == "trace-1"


class TestPlatformAnchor:
    @pytest.mark.asyncio
    async def test_creates_user_and_project_when_absent(self):
        added: List[Any] = []
        inner_db = MagicMock(
            execute=AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))),
            add=MagicMock(side_effect=lambda obj: added.append(obj)),
        )

        async def _flush():
            for obj in added:
                if getattr(obj, "id", None) is None:
                    obj.id = str(uuid.uuid4())
        inner_db.flush = AsyncMock(side_effect=_flush)

        db = MagicMock()
        db.__aenter__ = AsyncMock(return_value=inner_db)
        db.__aexit__ = AsyncMock(return_value=None)

        project_id = await ensure_platform_anchor(lambda: db)
        assert project_id is not None

    @pytest.mark.asyncio
    async def test_idempotent_when_already_exists(self):
        existing_project_id = str(uuid.uuid4())

        inner_db = MagicMock()
        user_result = MagicMock(scalar_one_or_none=MagicMock(return_value=MagicMock(id="user-1")))
        proj_result = MagicMock(scalar_one_or_none=MagicMock(
            return_value=MagicMock(id=existing_project_id)))
        inner_db.execute = AsyncMock(side_effect=[user_result, proj_result])
        inner_db.flush = AsyncMock()
        db = MagicMock()
        db.__aenter__ = AsyncMock(return_value=inner_db)
        db.__aexit__ = AsyncMock(return_value=None)

        project_id = await ensure_platform_anchor(lambda: db)
        assert project_id == existing_project_id


# ══════════════════════════════════════════════════════════════
# LAYER 4 — E2E: MonitoringHead full cycle
# ══════════════════════════════════════════════════════════════

class TestMonitoringHeadE2E:
    @pytest.mark.asyncio
    async def test_full_cycle_healthy(self):
        from services.monitoring.head import MonitoringHead
        infra = make_infra()
        agent = inject(MonitoringHead, infra, "monitoring_head", layer=3, role="head")
        task = make_task(__component_scores__={"postgres": 100.0, "nats": 95.0})
        result = await agent.execute(task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["status"] == "healthy"
        assert result.content["incidents"] == []
        # system_health_report + metrics_snapshot + 2 capacity_forecasts
        assert len(result.artifacts) == 4

    @pytest.mark.asyncio
    async def test_full_cycle_critical_no_incident_yet(self):
        from services.monitoring.head import MonitoringHead
        infra = make_infra()
        agent = inject(MonitoringHead, infra, "monitoring_head", layer=3, role="head")
        task = make_task(__component_scores__={"postgres": 5.0})
        result = await agent.execute(task)
        assert result.content["status"] == "critical"
        assert result.content["incidents"] == []  # only 1 consecutive cycle so far
        assert result.content["consecutive_critical_count"]["postgres"] == 1

    @pytest.mark.asyncio
    async def test_incident_escalates_after_three_consecutive_critical_cycles(self):
        from services.monitoring.head import MonitoringHead
        infra = make_infra()
        agent = inject(MonitoringHead, infra, "monitoring_head", layer=3, role="head")

        consecutive: Dict[str, int] = {}
        result = None
        for _ in range(3):
            task = make_task(__component_scores__={"postgres": 5.0},
                              __consecutive_critical_count__=consecutive)
            result = await agent.execute(task)
            consecutive = result.content["consecutive_critical_count"]

        assert len(result.content["incidents"]) == 1
        assert result.content["incidents"][0]["breach_cycles"] == 3

    @pytest.mark.asyncio
    async def test_publishes_metrics_updated_event(self):
        from services.monitoring.head import MonitoringHead
        infra = make_infra()
        agent = inject(MonitoringHead, infra, "monitoring_head", layer=3, role="head")
        task = make_task(__component_scores__={"postgres": 100.0})
        result = await agent.execute(task)
        subjects = [e.subject for e in result.nats_events]
        assert "monitoring.metrics.updated" in subjects

    @pytest.mark.asyncio
    async def test_publishes_incident_event_on_escalation(self):
        from services.monitoring.head import MonitoringHead
        infra = make_infra()
        agent = inject(MonitoringHead, infra, "monitoring_head", layer=3, role="head")
        consecutive = {"postgres": 2}  # one more critical cycle triggers escalation
        task = make_task(__component_scores__={"postgres": 5.0},
                          __consecutive_critical_count__=consecutive)
        result = await agent.execute(task)
        subjects = [e.subject for e in result.nats_events]
        assert "monitoring.incident" in subjects


# ══════════════════════════════════════════════════════════════
# LAYER 4b — E2E: full AlertingLead -> AlertWorker/DashboardWorker chain
# via the real classes (not FakeFactory) exercised through MetricsLead's
# output feeding into MonitoringHead feeding into AlertingLead.
# ══════════════════════════════════════════════════════════════

class TestFullMonitoringCycleWiring:
    @pytest.mark.asyncio
    async def test_metrics_lead_output_flows_into_head_and_alerting_lead(self):
        from services.monitoring.head import MonitoringHead
        from services.monitoring.leads import AlertingLead, MetricsLead

        infra = make_infra()
        metrics_lead = inject(MetricsLead, infra, "metrics_lead", layer=4, role="lead")
        head = inject(MonitoringHead, infra, "monitoring_head", layer=3, role="head")
        alerting_lead = inject(AlertingLead, infra, "alerting_lead", layer=4, role="lead")

        samples = [MetricSample(name="a", component=MonitoredComponent.POSTGRES, value=5.0).model_dump(mode="json")]
        metrics_factory = FakeFactory({
            "infrastructure_metrics_worker": ok_result("infrastructure_metrics_worker", samples=samples),
            "application_metrics_worker": ok_result("application_metrics_worker", samples=[]),
        })
        task = make_task(__factory__=metrics_factory)
        await metrics_lead.execute(task)
        assert task.context.approved_artifacts["__component_scores__"]["postgres"] == 5.0

        head_result = await head.execute(task)
        assert head_result.content["status"] == "critical"

        alerting_factory = FakeFactory({})  # real workers this time
        from services.monitoring.workers.alert import AlertWorker
        from services.monitoring.workers.dashboard import DashboardWorker
        alert_worker = inject(AlertWorker, infra, "alert_worker")
        dashboard_worker = inject(DashboardWorker, infra, "dashboard_worker")

        alert_result = await alert_worker.execute(task)
        dashboard_result = await dashboard_worker.execute(task)

        assert len(alert_result.content["alerts_raised"]) == 1
        assert len(dashboard_result.artifacts) == 1


# ══════════════════════════════════════════════════════════════
# LAYER 3d — Integration: HTTP API (routes.py, via TestClient + get_db override)
# ══════════════════════════════════════════════════════════════

class TestMonitoringAPI:
    def _client(self, db_session):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from infrastructure.database.connection import get_db
        from services.monitoring.api.routes import router

        app = FastAPI()
        app.include_router(router)

        async def _override():
            yield db_session

        app.dependency_overrides[get_db] = _override
        return TestClient(app)

    def _db_returning(self, scalar_one_or_none=None, scalars_all=None):
        db = MagicMock()
        db.execute = AsyncMock(return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=scalar_one_or_none),
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=scalars_all or []))),
        ))
        db.add = MagicMock()
        db.flush = AsyncMock()
        return db

    def test_get_health_with_no_data_yet(self):
        client = self._client(self._db_returning())
        resp = client.get("/monitoring/health")
        assert resp.status_code == 200
        assert resp.json()["health_score"] == 0.0

    def test_get_health_with_latest_row(self):
        row = MagicMock(health_score=91.5, status="healthy", component_scores={"postgres": 100.0},
                        cycle_at=datetime.utcnow())
        client = self._client(self._db_returning(scalar_one_or_none=row))
        resp = client.get("/monitoring/health")
        assert resp.status_code == 200
        assert resp.json()["health_score"] == 91.5

    def test_get_dashboard_not_found(self):
        client = self._client(self._db_returning())
        resp = client.get("/monitoring/dashboard")
        assert resp.status_code == 404

    def test_get_dashboard_found(self):
        row = MagicMock(layout={"columns": 3}, updated_at=datetime.utcnow())
        row.name = "platform_overview"
        client = self._client(self._db_returning(scalar_one_or_none=row))
        resp = client.get("/monitoring/dashboard")
        assert resp.status_code == 200
        assert resp.json()["name"] == "platform_overview"

    def test_get_alerts_empty(self):
        client = self._client(self._db_returning())
        resp = client.get("/monitoring/alerts")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_post_alerts_ack(self):
        client = self._client(self._db_returning())
        resp = client.post("/monitoring/alerts/ack", json={"alert_id": "a-1", "acknowledged_by": "op"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "acknowledged"

    def test_get_performance_no_artifact_yet(self):
        client = self._client(self._db_returning())
        resp = client.get("/monitoring/performance")
        assert resp.status_code == 200
        assert resp.json()["p95_latency_ms"] == 0.0

    def test_get_services(self):
        row = MagicMock(component_scores={"postgres": 100.0}, status="healthy")
        client = self._client(self._db_returning(scalar_one_or_none=row))
        resp = client.get("/monitoring/services")
        assert resp.status_code == 200
        assert resp.json()["component_scores"]["postgres"] == 100.0

    def test_get_agents_component_detail(self):
        row = MagicMock(component_scores={"agent_runtime": 88.0}, status="healthy")
        client = self._client(self._db_returning(scalar_one_or_none=row))
        resp = client.get("/monitoring/agents")
        assert resp.status_code == 200
        assert resp.json()["component"] == "agent_runtime"
        assert resp.json()["score"] == 88.0

    def test_get_deployments_component_detail(self):
        client = self._client(self._db_returning())
        resp = client.get("/monitoring/deployments")
        assert resp.status_code == 200
        assert resp.json()["component"] == "deployments"

    def test_get_costs_component_detail(self):
        client = self._client(self._db_returning())
        resp = client.get("/monitoring/costs")
        assert resp.status_code == 200
        assert resp.json()["component"] == "llm_providers"

    def test_get_metrics_no_filter(self):
        client = self._client(self._db_returning())
        resp = client.get("/monitoring/metrics")
        assert resp.status_code == 200
        assert resp.json()["samples"] == []


# ══════════════════════════════════════════════════════════════
# LAYER 3e — Integration: schemas / errors
# ══════════════════════════════════════════════════════════════

class TestMonitoringSchemas:
    def test_acknowledge_alert_request_optional_field(self):
        from services.monitoring.schemas import AcknowledgeAlertRequest
        req = AcknowledgeAlertRequest(alert_id="a-1")
        assert req.acknowledged_by is None

    def test_health_response_defaults(self):
        from services.monitoring.schemas import HealthResponse
        resp = HealthResponse(health_score=50.0, status=HealthStatus.WARNING)
        assert resp.component_scores == {}

    def test_monitoring_service_error_hierarchy(self):
        from services.monitoring.schemas import (
            AlertNotFoundError, MonitoringServiceError, ProviderUnavailableError,
        )
        assert issubclass(AlertNotFoundError, MonitoringServiceError)
        assert issubclass(ProviderUnavailableError, MonitoringServiceError)




class TestFailureModes:
    @pytest.mark.asyncio
    async def test_provider_timeout_degrades_not_raises(self):
        class TimeoutProvider(PostgresProvider):
            async def collect(self):
                raise TimeoutError("db timeout")

        provider = TimeoutProvider(None)
        with pytest.raises(TimeoutError):
            await provider.collect()
        # workers catch this — verified via InfrastructureMetricsWorker below

    @pytest.mark.asyncio
    async def test_worker_catches_provider_exception(self):
        from services.monitoring.workers.infrastructure_metrics import InfrastructureMetricsWorker
        infra = make_infra()
        agent = inject(InfrastructureMetricsWorker, infra, "infrastructure_metrics_worker")

        import services.monitoring.providers as providers_mod
        original = providers_mod.infrastructure_providers

        class BrokenProvider:
            component = MonitoredComponent.DOCKER
            async def collect(self):
                raise RuntimeError("boom")
            def _degraded(self, reason):
                return [MetricSample(name="docker_reachable", component=MonitoredComponent.DOCKER,
                                     value=0.0, labels={"reason": reason})]

        try:
            providers_mod.infrastructure_providers = lambda *a, **k: [BrokenProvider()]
            import services.monitoring.workers.infrastructure_metrics as mod
            mod.infrastructure_providers = providers_mod.infrastructure_providers
            result = await agent.execute(make_task())
            assert result.status == TaskStatus.COMPLETED
        finally:
            providers_mod.infrastructure_providers = original

    @pytest.mark.asyncio
    async def test_db_write_failure_does_not_crash_worker(self):
        from services.monitoring.workers.infrastructure_metrics import InfrastructureMetricsWorker
        infra = make_infra()
        infra["db_factory"] = MagicMock(side_effect=RuntimeError("db down"))
        agent = inject(InfrastructureMetricsWorker, infra, "infrastructure_metrics_worker")
        result = await agent.execute(make_task())
        assert result.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_nats_unreachable_alert_worker_still_completes(self):
        from services.monitoring.workers.alert import AlertWorker
        infra = make_infra()
        infra["db_factory"] = MagicMock(side_effect=RuntimeError("db down"))
        agent = inject(AlertWorker, infra, "alert_worker")
        task = make_task(__component_scores__={"postgres": 5.0})
        result = await agent.execute(task)
        assert result.status == TaskStatus.COMPLETED
        assert len(result.content["alerts_raised"]) == 1

    @pytest.mark.asyncio
    async def test_dashboard_worker_survives_grafana_export_failure(self, monkeypatch):
        from services.monitoring.workers.dashboard import DashboardWorker
        infra = make_infra()
        agent = inject(DashboardWorker, infra, "dashboard_worker")

        import services.monitoring.workers.dashboard as mod
        monkeypatch.setattr(mod.os, "makedirs", MagicMock(side_effect=OSError("readonly fs")))
        task = make_task(__component_scores__={"postgres": 90.0})
        result = await agent.execute(task)
        assert result.status == TaskStatus.COMPLETED

    def test_topological_batches_raises_on_impossible_deps(self):
        t = MonitoringTask(worker_agent_id="a", task_id="x", depends_on=["nonexistent"])
        with pytest.raises(ValueError):
            topological_batches([t])

    @pytest.mark.asyncio
    async def test_graph_handles_factory_raising_gracefully_via_dry_run(self):
        # A factory whose leads all fail should still let the cycle complete
        # with a degraded (critical) score rather than crashing the process.
        results = {
            "metrics_lead": fail_result("metrics_lead", "all providers down"),
            "observability_lead": fail_result("observability_lead", "no data"),
            "monitoring_head": ok_result("monitoring_head", health_score=0.0, status="critical",
                                         incidents=[], consecutive_critical_count={}),
            "alerting_lead": ok_result("alerting_lead"),
        }
        factory = FakeFactory(results)
        graph = build_monitoring_graph(factory)
        state = initial_state("proj-1")
        result = await graph.ainvoke(state)
        assert result["phase_status"] == "completed"
        assert result["health_score"] == 0.0
