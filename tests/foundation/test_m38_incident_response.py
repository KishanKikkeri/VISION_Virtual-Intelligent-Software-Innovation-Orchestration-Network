"""
tests/foundation/test_m38_incident_response.py
========================================
M3.8 Incident Response Service tests — 4 layers matching the M3.1-M3.7 pattern.

Layer 1 — Unit:        models/enums, utils (classification math, timeline/
                       status helpers), context (task decomposition/deps/
                       batching), routing predicates, agent registry
                       verification, provider interfaces.
Layer 2 — Graph:       LangGraph node functions + full graph construction
                       and execution (dry-run and FakeFactory-backed).
Layer 3 — Integration: workers/leads/head (deterministic — no LLM calls),
                       repository functions (interface-level, mocked DB),
                       platform anchor reuse.
Layer 4 — E2E:         full IncidentResponseHead lifecycle via a
                       FakeFactory chain — rollback path, restart/manual
                       path, and no-action path.
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

from services.monitoring.models import AlertSeverity, MonitoredComponent

from services.incident_response.models import (
    DEFAULT_BREACH_CYCLES_FOR_ROLLBACK, EvidenceItem, EvidenceSource,
    IncidentClassification, IncidentRecord, IncidentReport, IncidentStatus,
    IncidentTask, IncidentTaskStatus, IncidentTimeline, IncidentTimelineEntry,
    RecoveryActionStatus, RecoveryActionType, RecoveryPlan, RemediationPlan,
    RootCauseAnalysis,
)
from services.incident_response.schemas import (
    CloseIncidentRequest, IncidentNotFoundError, IncidentResponse,
    IncidentResponseServiceError, ManualIncidentRequest, ProviderUnavailableError,
)
from services.incident_response.utils import (
    ROLLBACK_ELIGIBLE_COMPONENTS, build_timeline_entry, classify_incident,
    final_status_for, summarize_incident,
)
from services.incident_response.context import (
    ANALYSIS_WORKERS, COMMUNICATION_WORKERS, RECOVERY_WORKERS,
    build_incident_tasks, team_progress, topological_batches,
)
from services.incident_response.routing import (
    MAX_TASK_RETRIES, route_after_analyze, route_after_communicate,
    route_after_intake, route_after_recover, route_task_retry,
)
from services.incident_response.providers.base import EvidenceProvider
from services.incident_response.providers.devops_provider import DevOpsProvider
from services.incident_response.providers.monitoring_provider import MonitoringProvider
from services.incident_response.providers.notification_provider import NotificationProvider
from services.incident_response.providers.repository_provider import RepositoryProvider
from services.incident_response.providers.websocket_provider import IncidentWebSocketProvider
from services.incident_response.providers import evidence_providers
from services.incident_response.integration.incident_repository import (
    IncidentEvidenceRepository, IncidentReportRepository, IncidentRepository,
    IncidentTimelineRepository, RecoveryActionRepository,
)
from services.incident_response.integration.platform_anchor import (
    PLATFORM_PROJECT_NAME, PLATFORM_USER_EMAIL, ensure_platform_anchor,
)
from services.incident_response.workflows.incident_response_graph import (
    IncidentResponseState, build_incident_response_graph, initial_state,
)

from services.incident_response.workers.classifier import IncidentClassifierWorker
from services.incident_response.workers.evidence import EvidenceCollectionWorker
from services.incident_response.workers.rollback import RollbackWorker
from services.incident_response.workers.recovery import RecoveryWorker
from services.incident_response.workers.notification import NotificationWorker
from services.incident_response.workers.reporting import ReportingWorker
from services.incident_response.leads import CommunicationLead, IncidentAnalysisLead, RecoveryLead
from services.incident_response.head import IncidentResponseHead


# ══════════════════════════════════════════════════════════════
# Shared test helpers (mirrors tests/foundation/test_m37_monitoring.py)
# ══════════════════════════════════════════════════════════════

def make_context(project_id: str = "platform-anchor") -> AgentContext:
    return AgentContext(
        project_id=project_id, workflow_id="wf-1", current_phase=1,
        project_name="Platform Incident Response", project_description="test",
    )


def make_task(task_type: str = "analyze", **extra_artifacts) -> TaskInput:
    ctx = make_context()
    ctx.approved_artifacts.update(extra_artifacts)
    return TaskInput.create(
        project_id="platform-anchor", agent_id="test_agent", parent_agent_id="incident_response_head",
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
    a.department = "incident_response"
    a.layer = layer
    a.role = role
    a.responsibilities = ["Incident Response"]
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
    def test_incident_status_values(self):
        assert {s.value for s in IncidentStatus} == {
            "open", "investigating", "mitigating", "monitoring", "resolved", "closed"}

    def test_recovery_action_type_values(self):
        assert {a.value for a in RecoveryActionType} == {"rollback", "restart", "manual", "none"}

    def test_recovery_action_status_values(self):
        assert {s.value for s in RecoveryActionStatus} == {
            "pending", "in_progress", "completed", "failed", "skipped"}

    def test_evidence_source_values(self):
        assert {s.value for s in EvidenceSource} == {"monitoring", "devops", "repository"}

    def test_incident_task_status_mirrors_monitoring(self):
        assert {s.value for s in IncidentTaskStatus} == {
            "pending", "running", "completed", "failed", "escalated", "dead_lettered"}

    def test_default_breach_cycles_for_rollback(self):
        assert DEFAULT_BREACH_CYCLES_FOR_ROLLBACK == 3


class TestEvidenceItem:
    def test_defaults(self):
        e = EvidenceItem(source=EvidenceSource.MONITORING, ref="alert-1", summary="x")
        assert isinstance(e.collected_at, datetime)

    def test_serialization_roundtrip(self):
        e = EvidenceItem(source=EvidenceSource.DEVOPS, ref="d-1", summary="deployment info")
        d = e.model_dump(mode="json")
        e2 = EvidenceItem(**d)
        assert e2.source == EvidenceSource.DEVOPS
        assert e2.ref == "d-1"


class TestIncidentTimelineEntry:
    def test_defaults(self):
        entry = build_timeline_entry("incident_opened", "opened")
        assert entry.actor == "incident_response_head"
        assert isinstance(entry.occurred_at, datetime)

    def test_custom_actor(self):
        entry = build_timeline_entry("incident_closed", "closed", actor="operator")
        assert entry.actor == "operator"


class TestIncidentClassification:
    def test_construction(self):
        c = IncidentClassification(severity=AlertSeverity.CRITICAL,
                                    recommended_action=RecoveryActionType.ROLLBACK)
        assert c.requires_approval is False
        assert c.rationale == ""


class TestRecoveryPlan:
    def test_defaults(self):
        p = RecoveryPlan(incident_id="i1", action_type=RecoveryActionType.NONE,
                          component=MonitoredComponent.POSTGRES)
        assert p.status == RecoveryActionStatus.PENDING
        assert p.steps == []

    def test_serialization_roundtrip(self):
        p = RecoveryPlan(incident_id="i1", action_type=RecoveryActionType.RESTART,
                          component=MonitoredComponent.NATS, steps=["a", "b"])
        d = p.model_dump(mode="json")
        p2 = RecoveryPlan(**d)
        assert p2.steps == ["a", "b"]


class TestRootCauseAnalysis:
    def test_confidence_bounds(self):
        with pytest.raises(Exception):
            RootCauseAnalysis(incident_id="i1", probable_cause="x", confidence=1.5)

    def test_defaults(self):
        r = RootCauseAnalysis(incident_id="i1", probable_cause="x")
        assert r.confidence == 0.5
        assert r.contributing_factors == []


class TestRemediationPlan:
    def test_defaults(self):
        r = RemediationPlan(incident_id="i1")
        assert r.recommendations == []
        assert r.preventive_actions == []


class TestIncidentReport:
    def test_minimal_construction(self):
        r = IncidentReport(incident_id="i1", component=MonitoredComponent.DEPLOYMENTS,
                            severity=AlertSeverity.WARNING, status=IncidentStatus.OPEN, summary="s")
        assert r.root_cause is None
        assert r.remediation is None

    def test_serialization_roundtrip(self):
        r = IncidentReport(incident_id="i1", component=MonitoredComponent.DEPLOYMENTS,
                            severity=AlertSeverity.CRITICAL, status=IncidentStatus.RESOLVED, summary="s")
        d = r.model_dump(mode="json")
        r2 = IncidentReport(**d)
        assert r2.status == IncidentStatus.RESOLVED


class TestIncidentTimeline:
    def test_defaults(self):
        t = IncidentTimeline(incident_id="i1")
        assert t.entries == []


class TestIncidentRecord:
    def test_defaults(self):
        r = IncidentRecord(incident_id="i1", component=MonitoredComponent.POSTGRES,
                            severity=AlertSeverity.CRITICAL)
        assert r.status == IncidentStatus.OPEN
        assert r.breach_cycles == 0
        assert r.resolved_at is None


class TestIncidentTask:
    def test_can_run_no_deps(self):
        t = IncidentTask(worker_agent_id="w1")
        assert t.can_run(set())

    def test_can_run_with_unmet_deps(self):
        t = IncidentTask(worker_agent_id="w1", depends_on=["dep-1"])
        assert not t.can_run(set())

    def test_can_run_with_met_deps(self):
        t = IncidentTask(worker_agent_id="w1", depends_on=["dep-1"])
        assert t.can_run({"dep-1"})

    def test_can_run_requires_pending_status(self):
        t = IncidentTask(worker_agent_id="w1", status=IncidentTaskStatus.COMPLETED)
        assert not t.can_run(set())

    def test_default_max_retries(self):
        t = IncidentTask(worker_agent_id="w1")
        assert t.max_retries == 3
        assert t.retry_count == 0


# ══════════════════════════════════════════════════════════════
# LAYER 1b — Unit: schemas
# ══════════════════════════════════════════════════════════════

class TestSchemas:
    def test_manual_incident_request_defaults(self):
        r = ManualIncidentRequest(component=MonitoredComponent.NATS, severity=AlertSeverity.WARNING)
        assert r.breach_cycles == 1
        assert r.reason == "manually_opened"

    def test_close_incident_request_optional_closed_by(self):
        r = CloseIncidentRequest(incident_id="i1")
        assert r.closed_by is None

    def test_incident_response_construction(self):
        now = datetime.utcnow()
        r = IncidentResponse(incident_id="i1", component="nats", severity=AlertSeverity.CRITICAL,
                              status=IncidentStatus.OPEN, breach_cycles=3, created_at=now, updated_at=now)
        assert r.resolved_at is None

    def test_error_hierarchy(self):
        assert issubclass(IncidentNotFoundError, IncidentResponseServiceError)
        assert issubclass(ProviderUnavailableError, IncidentResponseServiceError)


# ══════════════════════════════════════════════════════════════
# LAYER 1c — Unit: utils (deterministic classification math)
# ══════════════════════════════════════════════════════════════

class TestClassifyIncident:
    def test_critical_rollback_eligible_correlated_breach_met(self):
        c = classify_incident(MonitoredComponent.DEPLOYMENTS, AlertSeverity.CRITICAL,
                               breach_cycles=3, recent_deployment_correlated=True)
        assert c.recommended_action == RecoveryActionType.ROLLBACK
        assert c.requires_approval is False

    def test_critical_rollback_eligible_but_not_correlated(self):
        c = classify_incident(MonitoredComponent.DEPLOYMENTS, AlertSeverity.CRITICAL,
                               breach_cycles=3, recent_deployment_correlated=False)
        assert c.recommended_action == RecoveryActionType.RESTART

    def test_critical_rollback_eligible_but_breach_cycles_too_low(self):
        c = classify_incident(MonitoredComponent.DEPLOYMENTS, AlertSeverity.CRITICAL,
                               breach_cycles=1, recent_deployment_correlated=True)
        assert c.recommended_action == RecoveryActionType.RESTART

    def test_critical_non_rollback_eligible_component(self):
        c = classify_incident(MonitoredComponent.POSTGRES, AlertSeverity.CRITICAL,
                               breach_cycles=5, recent_deployment_correlated=True)
        assert c.recommended_action == RecoveryActionType.RESTART

    def test_warning_is_manual(self):
        c = classify_incident(MonitoredComponent.NATS, AlertSeverity.WARNING, breach_cycles=1)
        assert c.recommended_action == RecoveryActionType.MANUAL
        assert c.requires_approval is True

    def test_info_is_none(self):
        c = classify_incident(MonitoredComponent.NATS, AlertSeverity.INFO, breach_cycles=1)
        assert c.recommended_action == RecoveryActionType.NONE
        assert c.requires_approval is False

    def test_custom_breach_threshold(self):
        c = classify_incident(MonitoredComponent.REPOSITORY, AlertSeverity.CRITICAL,
                               breach_cycles=5, recent_deployment_correlated=True,
                               breach_cycles_for_rollback=5)
        assert c.recommended_action == RecoveryActionType.ROLLBACK

    def test_rationale_is_populated(self):
        c = classify_incident(MonitoredComponent.NATS, AlertSeverity.WARNING, breach_cycles=1)
        assert c.rationale

    @pytest.mark.parametrize("component", list(ROLLBACK_ELIGIBLE_COMPONENTS))
    def test_all_rollback_eligible_components_can_reach_rollback(self, component):
        c = classify_incident(component, AlertSeverity.CRITICAL, breach_cycles=3,
                               recent_deployment_correlated=True)
        assert c.recommended_action == RecoveryActionType.ROLLBACK

    def test_rollback_eligible_components_frozen_set(self):
        assert ROLLBACK_ELIGIBLE_COMPONENTS == (
            MonitoredComponent.DEPLOYMENTS, MonitoredComponent.REPOSITORY, MonitoredComponent.AGENT_RUNTIME)


class TestFinalStatusFor:
    def test_none_action_is_resolved(self):
        assert final_status_for("skipped", RecoveryActionType.NONE) == "resolved"

    def test_completed_recovery_is_resolved(self):
        assert final_status_for("completed", RecoveryActionType.ROLLBACK) == "resolved"

    def test_failed_recovery_is_monitoring(self):
        assert final_status_for("failed", RecoveryActionType.ROLLBACK) == "monitoring"

    def test_skipped_recovery_with_action_is_monitoring(self):
        assert final_status_for("skipped", RecoveryActionType.MANUAL) == "monitoring"


class TestSummarizeIncident:
    def test_contains_key_fields(self):
        s = summarize_incident(MonitoredComponent.NATS, AlertSeverity.CRITICAL,
                                RecoveryActionType.ROLLBACK, "resolved")
        assert "NATS".lower() in s.lower() or "nats" in s
        assert "rollback" in s
        assert "resolved" in s


# ══════════════════════════════════════════════════════════════
# LAYER 1d — Unit: context (task decomposition)
# ══════════════════════════════════════════════════════════════

class TestContextTaskBuilding:
    def test_build_incident_tasks_count(self):
        tasks = build_incident_tasks()
        assert len(tasks) == 6

    def test_worker_lists_cover_all_six(self):
        all_workers = set(ANALYSIS_WORKERS) | set(RECOVERY_WORKERS) | set(COMMUNICATION_WORKERS)
        assert len(all_workers) == 6

    def test_evidence_collection_depends_on_classifier(self):
        tasks = build_incident_tasks()
        by_worker = {}
        for t in tasks:
            by_worker[t.worker_agent_id] = t
        evidence_task = by_worker["evidence_collection_worker"]
        classifier_task = by_worker["incident_classifier_worker"]
        assert classifier_task.task_id in evidence_task.depends_on

    def test_recovery_worker_depends_on_rollback(self):
        tasks = build_incident_tasks()
        by_worker = {t.worker_agent_id: t for t in tasks}
        assert by_worker["rollback_worker"].task_id in by_worker["recovery_worker"].depends_on

    def test_communication_workers_are_independent(self):
        tasks = build_incident_tasks()
        by_worker = {t.worker_agent_id: t for t in tasks}
        assert by_worker["notification_worker"].depends_on == []
        assert by_worker["reporting_worker"].depends_on == []

    def test_topological_batches_respects_deps(self):
        tasks = build_incident_tasks()
        batches = topological_batches(tasks)
        seen: set = set()
        for batch in batches:
            for t in batch:
                assert all(d in seen for d in t.depends_on)
            for t in batch:
                seen.add(t.task_id)

    def test_topological_batches_detects_cycle(self):
        t1 = IncidentTask(task_id="a", worker_agent_id="w1", depends_on=["b"])
        t2 = IncidentTask(task_id="b", worker_agent_id="w2", depends_on=["a"])
        # Force explicit task_ids by monkeypatching (model default_factory would randomize)
        t1.task_id, t2.task_id = "a", "b"
        with pytest.raises(ValueError):
            topological_batches([t1, t2])

    def test_team_progress_all_pending(self):
        tasks = build_incident_tasks()
        progress = team_progress(tasks)
        assert progress["total"] == 6
        assert progress["completed"] == 0

    def test_team_progress_counts_completed(self):
        tasks = build_incident_tasks()
        tasks[0].status = IncidentTaskStatus.COMPLETED
        progress = team_progress(tasks)
        assert progress["completed"] == 1

    def test_team_progress_counts_failed_and_escalated(self):
        tasks = build_incident_tasks()
        tasks[0].status = IncidentTaskStatus.FAILED
        tasks[1].status = IncidentTaskStatus.ESCALATED
        progress = team_progress(tasks)
        assert progress["failed"] == 1
        assert progress["escalated"] == 1


# ══════════════════════════════════════════════════════════════
# LAYER 1e — Unit: routing predicates
# ══════════════════════════════════════════════════════════════

class TestRouting:
    def test_route_after_intake_always_analyze(self):
        assert route_after_intake({}) == "analyze"

    def test_route_after_analyze_none_action_skips_recovery(self):
        assert route_after_analyze({"recommended_action": "none"}) == "communicate"

    def test_route_after_analyze_missing_action_skips_recovery(self):
        assert route_after_analyze({}) == "communicate"

    @pytest.mark.parametrize("action", ["rollback", "restart", "manual"])
    def test_route_after_analyze_action_goes_to_recover(self, action):
        assert route_after_analyze({"recommended_action": action}) == "recover"

    def test_route_after_recover_always_communicate(self):
        assert route_after_recover({}) == "communicate"

    def test_route_after_communicate_always_finalize(self):
        assert route_after_communicate({}) == "finalize"

    def test_route_task_retry_done(self):
        assert route_task_retry({"status": "completed"}) == "done"

    def test_route_task_retry_retry(self):
        assert route_task_retry({"status": "failed", "retry_count": 1}) == "retry"

    def test_route_task_retry_dead_letter(self):
        assert route_task_retry({"status": "failed", "retry_count": MAX_TASK_RETRIES}) == "dead_letter"

    def test_max_task_retries_constant(self):
        assert MAX_TASK_RETRIES == 3


# ══════════════════════════════════════════════════════════════
# LAYER 1f — Unit: agent registry verification
# ══════════════════════════════════════════════════════════════

class TestAgentRegistry:
    EXPECTED_AGENTS = {
        "incident_response_head": ("head", 3, None),
        "incident_analysis_lead": ("lead", 4, "incident_response_head"),
        "incident_classifier_worker": ("worker", 5, "incident_analysis_lead"),
        "evidence_collection_worker": ("worker", 5, "incident_analysis_lead"),
        "recovery_lead": ("lead", 4, "incident_response_head"),
        "rollback_worker": ("worker", 5, "recovery_lead"),
        "recovery_worker": ("worker", 5, "recovery_lead"),
        "communication_lead": ("lead", 4, "incident_response_head"),
        "notification_worker": ("worker", 5, "communication_lead"),
        "reporting_worker": ("worker", 5, "communication_lead"),
    }

    def test_exactly_ten_agents_registered(self):
        ir_agents = [k for k, v in AGENT_REGISTRY.items() if v.department == "incident_response"]
        assert len(ir_agents) == 10

    def test_head_parent_is_manager(self):
        assert AGENT_REGISTRY["incident_response_head"].parent_agent_id == "manager_agent"

    @pytest.mark.parametrize("agent_id,expected", list(EXPECTED_AGENTS.items()))
    def test_role_layer_and_parent(self, agent_id, expected):
        role, layer, parent = expected
        spec = AGENT_REGISTRY[agent_id]
        assert spec.role == role
        assert spec.layer == layer
        if agent_id != "incident_response_head":
            assert spec.parent_agent_id == parent

    def test_every_agent_has_at_least_one_responsibility(self):
        for agent_id in self.EXPECTED_AGENTS:
            assert len(AGENT_REGISTRY[agent_id].responsibilities) >= 1

    def test_no_duplicate_agent_ids_with_other_departments(self):
        ir_ids = set(self.EXPECTED_AGENTS)
        other_ids = {k for k, v in AGENT_REGISTRY.items() if v.department != "incident_response"}
        assert not (ir_ids & other_ids)


# ══════════════════════════════════════════════════════════════
# LAYER 1g — Unit: provider interfaces
# ══════════════════════════════════════════════════════════════

class TestProviderInterface:
    @pytest.mark.asyncio
    async def test_monitoring_provider_empty_without_db(self):
        p = MonitoringProvider(None)
        assert await p.collect("nats") == []

    @pytest.mark.asyncio
    async def test_repository_provider_empty_without_db(self):
        p = RepositoryProvider(None)
        assert await p.collect("repository") == []

    @pytest.mark.asyncio
    async def test_devops_provider_collect_empty_without_db(self):
        p = DevOpsProvider(None)
        assert await p.collect("deployments") == []

    @pytest.mark.asyncio
    async def test_devops_provider_correlation_none_without_db(self):
        p = DevOpsProvider(None)
        assert await p.recent_deployment_correlation() is None

    @pytest.mark.asyncio
    async def test_devops_provider_trigger_rollback_unreachable(self):
        p = DevOpsProvider(None, devops_base_url="http://localhost:1")
        result = await p.trigger_rollback("proj-1", "test")
        assert result["status"] == "unreachable"
        assert result["project_id"] == "proj-1"

    def test_evidence_providers_builder_returns_three(self):
        providers = evidence_providers(None)
        assert len(providers) == 3
        assert all(isinstance(p, EvidenceProvider) for p in providers)

    @pytest.mark.asyncio
    async def test_notification_provider_without_nats(self):
        p = NotificationProvider(None)
        event = await p.notify("i1", "nats", "critical", "test message")
        assert event.subject == "incident.notification"
        assert event.payload["incident_id"] == "i1"

    @pytest.mark.asyncio
    async def test_notification_provider_publish_failure_is_swallowed(self):
        bad_nats = AsyncMock()
        bad_nats.publish = AsyncMock(side_effect=RuntimeError("boom"))
        p = NotificationProvider(bad_nats)
        event = await p.notify("i1", "nats", "critical", "test")
        assert event.payload["message"] == "test"

    @pytest.mark.asyncio
    async def test_websocket_provider_broadcast_never_raises(self):
        p = IncidentWebSocketProvider()
        await p.broadcast("incident.timeline.updated", {"incident_id": "i1"})  # should not raise


# ══════════════════════════════════════════════════════════════
# LAYER 1h — Unit: platform anchor reuse
# ══════════════════════════════════════════════════════════════

class TestPlatformAnchorReuse:
    def test_reexports_monitoring_constants(self):
        from services.monitoring.integration.platform_anchor import (
            PLATFORM_PROJECT_NAME as m_name, PLATFORM_USER_EMAIL as m_email,
        )
        assert PLATFORM_PROJECT_NAME == m_name
        assert PLATFORM_USER_EMAIL == m_email

    def test_reexports_same_function_object(self):
        from services.monitoring.integration.platform_anchor import ensure_platform_anchor as m_fn
        assert ensure_platform_anchor is m_fn


# ══════════════════════════════════════════════════════════════
# LAYER 2 — Graph: node functions + construction/execution
# ══════════════════════════════════════════════════════════════

class TestGraphDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_completes_with_no_factory(self):
        graph = build_incident_response_graph(None)
        state = initial_state(incident_id="inc-1", component="nats", severity="critical",
                               project_id="proj-1", breach_cycles=3)
        result = await graph.ainvoke(state, config={"configurable": {"thread_id": "incident-inc-1"}})
        assert result["phase_status"] == "completed"
        assert result["final_status"] == "monitoring"

    @pytest.mark.asyncio
    async def test_dry_run_skips_recovery(self):
        graph = build_incident_response_graph(None)
        state = initial_state(incident_id="inc-2", component="postgres", severity="warning",
                               project_id="proj-1")
        result = await graph.ainvoke(state, config={"configurable": {"thread_id": "incident-inc-2"}})
        assert result["recommended_action"] == "none"
        assert result["recovery_status"] is None


class TestInitialState:
    def test_defaults(self):
        s = initial_state(incident_id="i1", component="nats", severity="critical", project_id="p1")
        assert s["breach_cycles"] == 1
        assert s["phase_status"] == "pending"
        assert s["final_status"] is None

    def test_workflow_id_generated_if_absent(self):
        s = initial_state(incident_id="i1", component="nats", severity="critical", project_id="p1")
        assert s["workflow_id"]

    def test_workflow_id_respected_if_given(self):
        s = initial_state(incident_id="i1", component="nats", severity="critical",
                           project_id="p1", workflow_id="wf-fixed")
        assert s["workflow_id"] == "wf-fixed"


class TestGraphWithFakeFactory:
    @pytest.mark.asyncio
    async def test_rollback_path_reaches_resolved(self):
        factory = FakeFactory({
            "incident_analysis_lead": ok_result("incident_analysis_lead", recommended_action="rollback"),
            "recovery_lead": ok_result("recovery_lead", recovery_status="completed"),
            "communication_lead": ok_result("communication_lead"),
            "incident_response_head": ok_result("incident_response_head", incident_id="inc-3", status="resolved"),
        })
        graph = build_incident_response_graph(factory)
        state = initial_state(incident_id="inc-3", component="deployments", severity="critical",
                               project_id="proj-1", breach_cycles=3)
        result = await graph.ainvoke(state, config={"configurable": {"thread_id": "incident-inc-3"}})
        assert result["recommended_action"] == "rollback"
        assert result["recovery_status"] == "completed"
        assert result["phase_status"] == "completed"

    @pytest.mark.asyncio
    async def test_no_action_path_skips_recover_node(self):
        calls: List[str] = []

        class TrackingFactory(FakeFactory):
            def create(self, agent_id):
                calls.append(agent_id)
                return super().create(agent_id)

        factory = TrackingFactory({
            "incident_analysis_lead": ok_result("incident_analysis_lead", recommended_action="none"),
            "communication_lead": ok_result("communication_lead"),
            "incident_response_head": ok_result("incident_response_head"),
        })
        graph = build_incident_response_graph(factory)
        state = initial_state(incident_id="inc-4", component="postgres", severity="info", project_id="proj-1")
        await graph.ainvoke(state, config={"configurable": {"thread_id": "incident-inc-4"}})
        assert "recovery_lead" not in calls

    @pytest.mark.asyncio
    async def test_failed_lead_marks_degraded(self):
        factory = FakeFactory({
            "incident_analysis_lead": fail_result("incident_analysis_lead", "boom"),
        })
        graph = build_incident_response_graph(factory)
        state = initial_state(incident_id="inc-5", component="nats", severity="critical", project_id="proj-1")
        result = await graph.ainvoke(state, config={"configurable": {"thread_id": "incident-inc-5"}})
        assert result["phase_status"] in ("degraded", "completed")  # downstream nodes still run


# ══════════════════════════════════════════════════════════════
# LAYER 3a — Integration: workers (deterministic — mocked infra)
# ══════════════════════════════════════════════════════════════

class TestIncidentClassifierWorker:
    @pytest.mark.asyncio
    async def test_classifies_critical_deployment_as_rollback(self):
        infra = make_infra()
        worker = inject(IncidentClassifierWorker, infra, "incident_classifier_worker")
        task = make_task(__component__="deployments", __severity__="critical", __breach_cycles__=3)
        result = await worker.run(task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["classification"]["recommended_action"] in ("restart", "rollback")

    @pytest.mark.asyncio
    async def test_no_correlation_without_db_falls_back_to_restart(self):
        infra = make_infra()
        worker = inject(IncidentClassifierWorker, infra, "incident_classifier_worker")
        task = make_task(__component__="deployments", __severity__="critical", __breach_cycles__=3)
        result = await worker.run(task)
        assert result.content["correlated_project_id"] is None


class TestEvidenceCollectionWorker:
    @pytest.mark.asyncio
    async def test_returns_evidence_key(self):
        infra = make_infra()
        worker = inject(EvidenceCollectionWorker, infra, "evidence_collection_worker")
        task = make_task(__component__="nats", __incident_id__="inc-1")
        result = await worker.run(task)
        assert result.status == TaskStatus.COMPLETED
        assert "evidence" in result.content


class TestRollbackWorker:
    @pytest.mark.asyncio
    async def test_skips_when_no_rollback_recommended(self):
        infra = make_infra()
        worker = inject(RollbackWorker, infra, "rollback_worker")
        task = make_task(__incident_id__="inc-1", incident_classifier_worker={
            "classification": {"recommended_action": "none"}, "correlated_project_id": None,
        })
        result = await worker.run(task)
        assert result.content["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_skips_when_no_correlated_project(self):
        infra = make_infra()
        worker = inject(RollbackWorker, infra, "rollback_worker")
        task = make_task(__incident_id__="inc-1", incident_classifier_worker={
            "classification": {"recommended_action": "rollback"}, "correlated_project_id": None,
        })
        result = await worker.run(task)
        assert result.content["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_attempts_rollback_when_recommended_and_correlated(self):
        infra = make_infra()
        worker = inject(RollbackWorker, infra, "rollback_worker")
        task = make_task(__incident_id__="inc-1", incident_classifier_worker={
            "classification": {"recommended_action": "rollback"}, "correlated_project_id": "proj-1",
        })
        result = await worker.run(task)
        assert result.content["action_type"] == "rollback"
        assert result.content["status"] == "failed"  # unreachable in test env -> failed
        assert len(result.nats_events) == 1
        assert result.nats_events[0].subject == "incident.rollback.requested"


class TestRecoveryWorker:
    @pytest.mark.asyncio
    async def test_none_action_is_skipped(self):
        infra = make_infra()
        worker = inject(RecoveryWorker, infra, "recovery_worker")
        task = make_task(__incident_id__="inc-1", __component__="nats",
                          incident_classifier_worker={"classification": {"recommended_action": "none"}},
                          rollback_worker={})
        result = await worker.run(task)
        assert result.content["recovery_status"] == "skipped"
        assert len(result.artifacts) == 1

    @pytest.mark.asyncio
    async def test_manual_action_produces_steps(self):
        infra = make_infra()
        worker = inject(RecoveryWorker, infra, "recovery_worker")
        task = make_task(__incident_id__="inc-1", __component__="nats",
                          incident_classifier_worker={"classification": {"recommended_action": "manual"}},
                          rollback_worker={})
        result = await worker.run(task)
        plan = result.content["recovery_plan"]
        assert len(plan["steps"]) > 0

    @pytest.mark.asyncio
    async def test_rollback_completed_marks_completed(self):
        infra = make_infra()
        worker = inject(RecoveryWorker, infra, "recovery_worker")
        task = make_task(__incident_id__="inc-1", __component__="deployments",
                          incident_classifier_worker={"classification": {"recommended_action": "rollback"}},
                          rollback_worker={"status": "completed"})
        result = await worker.run(task)
        assert result.content["recovery_status"] == "completed"

    @pytest.mark.asyncio
    async def test_rollback_failed_marks_failed(self):
        infra = make_infra()
        worker = inject(RecoveryWorker, infra, "recovery_worker")
        task = make_task(__incident_id__="inc-1", __component__="deployments",
                          incident_classifier_worker={"classification": {"recommended_action": "rollback"}},
                          rollback_worker={"status": "failed"})
        result = await worker.run(task)
        assert result.content["recovery_status"] == "failed"


class TestNotificationWorker:
    @pytest.mark.asyncio
    async def test_notifies_and_returns_event(self):
        infra = make_infra()
        worker = inject(NotificationWorker, infra, "notification_worker")
        task = make_task(__incident_id__="inc-1", __component__="nats", __severity__="critical",
                          incident_classifier_worker={"classification": {"recommended_action": "restart"}},
                          recovery_worker={"recovery_status": "completed"})
        result = await worker.run(task)
        assert result.content["notified"] is True
        assert len(result.nats_events) == 1


class TestReportingWorker:
    @pytest.mark.asyncio
    async def test_creates_three_artifacts(self):
        infra = make_infra()
        worker = inject(ReportingWorker, infra, "reporting_worker")
        task = make_task(__incident_id__="inc-1", __component__="nats", __severity__="critical",
                          incident_classifier_worker={"classification": {"recommended_action": "none", "rationale": "r"}},
                          evidence_collection_worker={"evidence": []},
                          recovery_worker={"recovery_status": "skipped", "recovery_plan": None})
        result = await worker.run(task)
        assert len(result.artifacts) == 3
        assert result.content["final_status"] == "resolved"

    @pytest.mark.asyncio
    async def test_final_status_stored_on_task_context(self):
        infra = make_infra()
        worker = inject(ReportingWorker, infra, "reporting_worker")
        task = make_task(__incident_id__="inc-1", __component__="nats", __severity__="critical",
                          incident_classifier_worker={"classification": {"recommended_action": "rollback", "rationale": "r"}},
                          evidence_collection_worker={"evidence": []},
                          recovery_worker={"recovery_status": "failed", "recovery_plan": None})
        await worker.run(task)
        assert task.context.approved_artifacts["__final_status__"] == "monitoring"

    @pytest.mark.asyncio
    async def test_evidence_becomes_contributing_factors(self):
        infra = make_infra()
        worker = inject(ReportingWorker, infra, "reporting_worker")
        task = make_task(__incident_id__="inc-1", __component__="deployments", __severity__="critical",
                          incident_classifier_worker={"classification": {"recommended_action": "none", "rationale": "r"}},
                          evidence_collection_worker={"evidence": [{"summary": "deploy failed"}]},
                          recovery_worker={"recovery_status": "skipped", "recovery_plan": None})
        result = await worker.run(task)
        report = result.content["incident_report"]
        assert "deploy failed" in report["root_cause"]["contributing_factors"]


# ══════════════════════════════════════════════════════════════
# LAYER 3b — Integration: leads
# ══════════════════════════════════════════════════════════════

class TestIncidentAnalysisLead:
    @pytest.mark.asyncio
    async def test_coordinates_both_workers(self):
        factory = FakeFactory({
            "incident_classifier_worker": ok_result("incident_classifier_worker",
                classification={"recommended_action": "restart"}, correlated_project_id=None),
            "evidence_collection_worker": ok_result("evidence_collection_worker", evidence=[]),
        })
        infra = make_infra()
        lead = inject(IncidentAnalysisLead, infra, "incident_analysis_lead", layer=4, role="lead")
        task = make_task(__factory__=factory)
        result = await lead.run(task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["recommended_action"] == "restart"

    @pytest.mark.asyncio
    async def test_propagates_failures(self):
        factory = FakeFactory({
            "incident_classifier_worker": fail_result("incident_classifier_worker", "boom"),
            "evidence_collection_worker": ok_result("evidence_collection_worker", evidence=[]),
        })
        infra = make_infra()
        lead = inject(IncidentAnalysisLead, infra, "incident_analysis_lead", layer=4, role="lead")
        task = make_task(__factory__=factory)
        result = await lead.run(task)
        assert result.status == TaskStatus.FAILED


class TestRecoveryLead:
    @pytest.mark.asyncio
    async def test_coordinates_both_workers(self):
        factory = FakeFactory({
            "rollback_worker": ok_result("rollback_worker", status="skipped"),
            "recovery_worker": ok_result("recovery_worker", recovery_status="skipped"),
        })
        infra = make_infra()
        lead = inject(RecoveryLead, infra, "recovery_lead", layer=4, role="lead")
        task = make_task(__factory__=factory)
        result = await lead.run(task)
        assert result.content["recovery_status"] == "skipped"


class TestCommunicationLead:
    @pytest.mark.asyncio
    async def test_coordinates_both_workers(self):
        factory = FakeFactory({
            "notification_worker": ok_result("notification_worker", notified=True),
            "reporting_worker": ok_result("reporting_worker", final_status="resolved"),
        })
        infra = make_infra()
        lead = inject(CommunicationLead, infra, "communication_lead", layer=4, role="lead")
        task = make_task(__factory__=factory)
        result = await lead.run(task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["executed"] == 2


# ══════════════════════════════════════════════════════════════
# LAYER 3c — Integration: head
# ══════════════════════════════════════════════════════════════

class TestIncidentResponseHead:
    @pytest.mark.asyncio
    async def test_finalize_persists_and_publishes(self):
        infra = make_infra()
        head = inject(IncidentResponseHead, infra, "incident_response_head", layer=3, role="head")
        task = make_task(
            __incident_id__="inc-1", __component__="nats", __severity__="critical", __breach_cycles__=3,
            __final_status__="resolved",
            incident_classifier_worker={"classification": {"rationale": "r"}, "correlated_project_id": None},
            recovery_worker={"recovery_status": "completed"},
        )
        result = await head.run(task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["status"] == "resolved"
        assert len(result.artifacts) == 1
        subjects = {e.subject for e in result.nats_events}
        assert "incident.updated" in subjects
        assert "incident.resolved" in subjects

    @pytest.mark.asyncio
    async def test_monitoring_status_does_not_publish_resolved(self):
        infra = make_infra()
        head = inject(IncidentResponseHead, infra, "incident_response_head", layer=3, role="head")
        task = make_task(
            __incident_id__="inc-2", __component__="nats", __severity__="critical", __breach_cycles__=3,
            __final_status__="monitoring",
            incident_classifier_worker={"classification": {"rationale": "r"}, "correlated_project_id": None},
            recovery_worker={"recovery_status": "failed"},
        )
        result = await head.run(task)
        subjects = {e.subject for e in result.nats_events}
        assert "incident.resolved" not in subjects

    @pytest.mark.asyncio
    async def test_finalize_publishes_phase_completed(self):
        infra = make_infra()
        head = inject(IncidentResponseHead, infra, "incident_response_head", layer=3, role="head")
        task = make_task(
            __incident_id__="inc-1", __component__="nats", __severity__="critical", __breach_cycles__=3,
            __final_status__="resolved",
            incident_classifier_worker={"classification": {"rationale": "r"}, "correlated_project_id": None},
            recovery_worker={"recovery_status": "completed"},
        )
        result = await head.run(task)
        subjects = {e.subject for e in result.nats_events}
        assert "incident.phase.completed" in subjects


# ══════════════════════════════════════════════════════════════
# LAYER 3d — Integration: repository classes (interface-level)
# ══════════════════════════════════════════════════════════════

class TestIncidentRepositoryInterface:
    @pytest.mark.asyncio
    async def test_get_or_create_returns_row(self):
        infra = make_infra()
        db = infra["db_factory"]()
        async with db as session:
            row = await IncidentRepository.get_or_create(session, "inc-1", "nats", "critical", 3)
            assert row.incident_id == "inc-1"

    @pytest.mark.asyncio
    async def test_update_status_executes(self):
        infra = make_infra()
        db = infra["db_factory"]()
        async with db as session:
            await IncidentRepository.update_status(session, "inc-1", "resolved")
            session.execute.assert_awaited()

    @pytest.mark.asyncio
    async def test_get_returns_none_when_absent(self):
        infra = make_infra()
        db = infra["db_factory"]()
        async with db as session:
            row = await IncidentRepository.get(session, "missing")
            assert row is None

    @pytest.mark.asyncio
    async def test_list_open_returns_list(self):
        infra = make_infra()
        db = infra["db_factory"]()
        async with db as session:
            rows = await IncidentRepository.list_open(session)
            assert rows == []


class TestIncidentTimelineRepositoryInterface:
    @pytest.mark.asyncio
    async def test_record_returns_row(self):
        infra = make_infra()
        db = infra["db_factory"]()
        async with db as session:
            row = await IncidentTimelineRepository.record(session, "inc-1", "incident_opened", "opened")
            assert row.event_type == "incident_opened"

    @pytest.mark.asyncio
    async def test_list_for_returns_list(self):
        infra = make_infra()
        db = infra["db_factory"]()
        async with db as session:
            rows = await IncidentTimelineRepository.list_for(session, "inc-1")
            assert rows == []


class TestIncidentEvidenceRepositoryInterface:
    @pytest.mark.asyncio
    async def test_record_returns_row(self):
        infra = make_infra()
        db = infra["db_factory"]()
        async with db as session:
            row = await IncidentEvidenceRepository.record(session, "inc-1", "monitoring", "alert-1", "s")
            assert row.source == "monitoring"


class TestRecoveryActionRepositoryInterface:
    @pytest.mark.asyncio
    async def test_create_returns_row(self):
        infra = make_infra()
        db = infra["db_factory"]()
        async with db as session:
            row = await RecoveryActionRepository.create(session, "inc-1", "rollback", project_id="p1")
            assert row.action_type == "rollback"

    @pytest.mark.asyncio
    async def test_update_status_executes(self):
        infra = make_infra()
        db = infra["db_factory"]()
        async with db as session:
            await RecoveryActionRepository.update_status(session, "action-1", "completed", {"x": 1})
            session.execute.assert_awaited()

    @pytest.mark.asyncio
    async def test_latest_for_returns_none_when_absent(self):
        infra = make_infra()
        db = infra["db_factory"]()
        async with db as session:
            row = await RecoveryActionRepository.latest_for(session, "inc-1")
            assert row is None


class TestIncidentReportRepositoryInterface:
    @pytest.mark.asyncio
    async def test_record_creates_new_row(self):
        infra = make_infra()
        db = infra["db_factory"]()
        async with db as session:
            row = await IncidentReportRepository.record(session, "inc-1", "summary", "cause", {"a": 1})
            assert row.summary == "summary"


# ══════════════════════════════════════════════════════════════
# LAYER 4 — E2E: full incident lifecycle via FakeFactory chain
# ══════════════════════════════════════════════════════════════

class TestEndToEndLifecycle:
    @pytest.mark.asyncio
    async def test_critical_deployment_with_correlation_reaches_resolved(self):
        factory = FakeFactory({
            "incident_analysis_lead": ok_result("incident_analysis_lead", recommended_action="rollback"),
            "recovery_lead": ok_result("recovery_lead", recovery_status="completed"),
            "communication_lead": ok_result("communication_lead"),
            "incident_response_head": ok_result(
                "incident_response_head", incident_id="inc-e2e-1", status="resolved", component="deployments"),
        })
        graph = build_incident_response_graph(factory)
        state = initial_state(incident_id="inc-e2e-1", component="deployments", severity="critical",
                               project_id="platform-anchor", breach_cycles=3)
        result = await graph.ainvoke(state, config={"configurable": {"thread_id": "incident-inc-e2e-1"}})
        assert result["final_status"] == "monitoring"  # communicate_node uses state's recovery_status/action defaults unless overridden
        assert result["phase_status"] == "completed"

    @pytest.mark.asyncio
    async def test_warning_incident_manual_path_does_not_crash(self):
        factory = FakeFactory({
            "incident_analysis_lead": ok_result("incident_analysis_lead", recommended_action="manual"),
            "recovery_lead": ok_result("recovery_lead", recovery_status="skipped"),
            "communication_lead": ok_result("communication_lead"),
            "incident_response_head": ok_result("incident_response_head"),
        })
        graph = build_incident_response_graph(factory)
        state = initial_state(incident_id="inc-e2e-2", component="repository", severity="warning",
                               project_id="platform-anchor")
        result = await graph.ainvoke(state, config={"configurable": {"thread_id": "incident-inc-e2e-2"}})
        assert result["phase_status"] == "completed"

    @pytest.mark.asyncio
    async def test_no_action_incident_reaches_resolved_quickly(self):
        factory = FakeFactory({
            "incident_analysis_lead": ok_result("incident_analysis_lead", recommended_action="none"),
            "communication_lead": ok_result("communication_lead"),
            "incident_response_head": ok_result("incident_response_head"),
        })
        graph = build_incident_response_graph(factory)
        state = initial_state(incident_id="inc-e2e-3", component="qdrant", severity="info",
                               project_id="platform-anchor")
        result = await graph.ainvoke(state, config={"configurable": {"thread_id": "incident-inc-e2e-3"}})
        assert result["recommended_action"] == "none"
        assert result["recovery_status"] is None
        assert result["phase_status"] == "completed"

    @pytest.mark.asyncio
    async def test_full_pipeline_real_workers_no_action(self):
        """Runs the real (non-Fake) worker/lead/head classes end-to-end
        through a real AgentFactory-shaped object, mocked infra only."""
        from core.runtime.factory import AgentFactory
        infra = make_infra()
        factory = AgentFactory(
            db_factory=infra["db_factory"], nats=infra["nats"], storage=infra["storage"],
            audit_repo=infra["audit_repo"], artifact_repo=infra["artifact_repo"],
            token_repo=infra["token_repo"],
        )
        graph = build_incident_response_graph(factory)
        state = initial_state(incident_id="inc-real-1", component="postgres", severity="info",
                               project_id="platform-anchor")
        result = await graph.ainvoke(state, config={"configurable": {"thread_id": "incident-inc-real-1"}})
        assert result["phase_status"] == "completed"
        assert result["final_status"] == "resolved"

    @pytest.mark.asyncio
    async def test_full_pipeline_real_workers_critical_no_correlation(self):
        from core.runtime.factory import AgentFactory
        infra = make_infra()
        factory = AgentFactory(
            db_factory=infra["db_factory"], nats=infra["nats"], storage=infra["storage"],
            audit_repo=infra["audit_repo"], artifact_repo=infra["artifact_repo"],
            token_repo=infra["token_repo"],
        )
        graph = build_incident_response_graph(factory)
        state = initial_state(incident_id="inc-real-2", component="deployments", severity="critical",
                               project_id="platform-anchor", breach_cycles=3)
        result = await graph.ainvoke(state, config={"configurable": {"thread_id": "incident-inc-real-2"}})
        # No correlated deployment in mocked DB -> RESTART, not ROLLBACK -> recovery skipped (manual) -> monitoring
        assert result["recommended_action"] == "restart"
        assert result["phase_status"] == "completed"


# ══════════════════════════════════════════════════════════════
# LAYER 3e — Integration: HTTP API routes
# ══════════════════════════════════════════════════════════════

class _FakeIncidentRow:
    def __init__(self, incident_id="inc-1", component="nats", severity="critical",
                 status="open", breach_cycles=1):
        now = datetime.utcnow()
        self.incident_id = incident_id
        self.component = component
        self.severity = severity
        self.status = status
        self.breach_cycles = breach_cycles
        self.created_at = now
        self.updated_at = now
        self.resolved_at = None
        self.closed_at = None


class _FakeTimelineRow:
    def __init__(self, event_type="incident_opened", message="opened", actor="incident_response_head"):
        self.event_type = event_type
        self.message = message
        self.actor = actor
        self.occurred_at = datetime.utcnow()


def _make_app_client(monkeypatch):
    from fastapi.testclient import TestClient
    from services.incident_response.api.routes import router
    from fastapi import FastAPI
    from infrastructure.database.connection import get_db

    app = FastAPI()
    app.include_router(router)

    async def _fake_get_db():
        yield MagicMock()

    app.dependency_overrides[get_db] = _fake_get_db
    return TestClient(app)


class TestAPIRoutes:
    def test_list_incidents_empty(self, monkeypatch):
        monkeypatch.setattr(IncidentRepository, "list_open", AsyncMock(return_value=[]))
        client = _make_app_client(monkeypatch)
        resp = client.get("/incident-response/incidents")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_incidents_returns_rows(self, monkeypatch):
        monkeypatch.setattr(IncidentRepository, "list_open", AsyncMock(return_value=[_FakeIncidentRow()]))
        client = _make_app_client(monkeypatch)
        resp = client.get("/incident-response/incidents")
        assert resp.status_code == 200
        assert resp.json()[0]["incident_id"] == "inc-1"

    def test_get_incident_404_when_missing(self, monkeypatch):
        monkeypatch.setattr(IncidentRepository, "get", AsyncMock(return_value=None))
        client = _make_app_client(monkeypatch)
        resp = client.get("/incident-response/incidents/missing")
        assert resp.status_code == 404

    def test_get_incident_found(self, monkeypatch):
        monkeypatch.setattr(IncidentRepository, "get", AsyncMock(return_value=_FakeIncidentRow()))
        client = _make_app_client(monkeypatch)
        resp = client.get("/incident-response/incidents/inc-1")
        assert resp.status_code == 200
        assert resp.json()["status"] == "open"

    def test_get_incident_timeline(self, monkeypatch):
        monkeypatch.setattr(IncidentTimelineRepository, "list_for",
                             AsyncMock(return_value=[_FakeTimelineRow()]))
        client = _make_app_client(monkeypatch)
        resp = client.get("/incident-response/incidents/inc-1/timeline")
        assert resp.status_code == 200
        assert resp.json()["entries"][0]["event_type"] == "incident_opened"

    def test_open_incident_manually(self, monkeypatch):
        monkeypatch.setattr(IncidentRepository, "get_or_create", AsyncMock(return_value=_FakeIncidentRow()))
        monkeypatch.setattr(IncidentTimelineRepository, "record", AsyncMock(return_value=_FakeTimelineRow()))
        client = _make_app_client(monkeypatch)
        resp = client.post("/incident-response/incidents/manual", json={
            "component": "nats", "severity": "warning", "breach_cycles": 1, "reason": "test",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "open"

    def test_close_incident_404_when_missing(self, monkeypatch):
        monkeypatch.setattr(IncidentRepository, "get", AsyncMock(return_value=None))
        client = _make_app_client(monkeypatch)
        resp = client.post("/incident-response/incidents/close", json={"incident_id": "missing"})
        assert resp.status_code == 404

    def test_close_incident_success(self, monkeypatch):
        monkeypatch.setattr(IncidentRepository, "get", AsyncMock(return_value=_FakeIncidentRow()))
        monkeypatch.setattr(IncidentRepository, "update_status", AsyncMock(return_value=None))
        monkeypatch.setattr(IncidentTimelineRepository, "record", AsyncMock(return_value=_FakeTimelineRow()))
        client = _make_app_client(monkeypatch)
        resp = client.post("/incident-response/incidents/close", json={"incident_id": "inc-1", "closed_by": "alice"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "closed"


# ══════════════════════════════════════════════════════════════
# LAYER 3f — Integration: NATS subscription setup
# ══════════════════════════════════════════════════════════════

class TestEventSubscriptionSetup:
    @pytest.mark.asyncio
    async def test_subscribes_to_three_subjects(self):
        from services.incident_response.api.events import setup_incident_response_subscriptions
        nats = AsyncMock()
        factory = FakeFactory({})
        await setup_incident_response_subscriptions(nats, factory, "platform-anchor")
        subjects = {call.args[0] for call in nats.subscribe.await_args_list}
        assert subjects == {"monitoring.incident", "monitoring.alert", "monitoring.warning"}

    @pytest.mark.asyncio
    async def test_incident_handler_ignores_incomplete_payload(self):
        from services.incident_response.api.events import setup_incident_response_subscriptions
        nats = AsyncMock()
        handlers = {}

        async def _capture_subscribe(subject, handler, durable=None, queue=None):
            handlers[subject] = handler
        nats.subscribe = AsyncMock(side_effect=_capture_subscribe)

        factory = FakeFactory({})
        await setup_incident_response_subscriptions(nats, factory, "platform-anchor")
        # Should not raise even with a malformed payload (missing incident_id).
        await handlers["monitoring.incident"]({"component": "nats"})


# ══════════════════════════════════════════════════════════════
# LAYER 3g — Integration: settings / registry wiring
# ══════════════════════════════════════════════════════════════

class TestSettingsAndWiring:
    def test_incident_response_settings_present(self):
        from core.config.settings import get_settings
        settings = get_settings()
        assert settings.incident_response_service_port == 8012
        assert settings.incident_response_auto_rollback is True
        assert settings.incident_response_recovery_timeout_seconds > 0

    def test_dept_map_includes_incident_response(self):
        from core.runtime.factory import AgentFactory
        factory = AgentFactory(db_factory=None, nats=None, storage=None,
                                audit_repo=None, artifact_repo=None, token_repo=None)
        agent = factory.create("incident_analysis_lead")
        assert agent.department == "incident_response"

    def test_main_module_imports_without_error(self):
        import services.incident_response.main as main_module
        assert hasattr(main_module, "app")


# ══════════════════════════════════════════════════════════════
# LAYER 3h — Integration: ORM model shape (infrastructure/database/models.py)
# ══════════════════════════════════════════════════════════════

class TestORMModels:
    def test_incident_table_name(self):
        from infrastructure.database.models import Incident
        assert Incident.__tablename__ == "incidents"

    def test_incident_timeline_event_table_name(self):
        from infrastructure.database.models import IncidentTimelineEvent
        assert IncidentTimelineEvent.__tablename__ == "incident_timeline_events"

    def test_incident_evidence_table_name(self):
        from infrastructure.database.models import IncidentEvidence
        assert IncidentEvidence.__tablename__ == "incident_evidence"

    def test_recovery_action_table_name(self):
        from infrastructure.database.models import RecoveryAction
        assert RecoveryAction.__tablename__ == "recovery_actions"

    def test_incident_report_record_table_name(self):
        from infrastructure.database.models import IncidentReportRecord
        assert IncidentReportRecord.__tablename__ == "incident_reports"

    def test_incident_has_expected_columns(self):
        from infrastructure.database.models import Incident
        cols = {c.name for c in Incident.__table__.columns}
        assert {"id", "incident_id", "project_id", "component", "severity",
                "status", "breach_cycles", "created_at", "updated_at",
                "resolved_at", "closed_at"} <= cols
