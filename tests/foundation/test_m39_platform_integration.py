"""
tests/foundation/test_m39_platform_integration.py
========================================
M3.9 Platform Integration tests — mirrors the M3.7/M3.8 layered pattern.

Layer 1 — Unit:        dependency_graph, artifact_validator, event_router
                       manifests and pure functions, health_validator
                       pure helpers, lifecycle's reachability algorithm.
Layer 2 — Integration: health_validator's async checks (mocked infra),
                       orchestrator's registry/readiness computation,
                       repository classes (mocked DB), startup checks.
Layer 3 — API:         all 7 /platform/* routes via TestClient.
Layer 4 — E2E:         full generate_full_report() against the real,
                       live platform (real AGENT_REGISTRY, real
                       LangGraph builders, real event manifest) — this
                       is the one place these tests double as an
                       actual platform validation run, not just a
                       test of the validator's own logic.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.runtime.factory import AGENT_REGISTRY, AgentFactory

from services.integration import artifact_validator, dependency_graph, event_router, health_validator, lifecycle
from services.integration import orchestrator as orchestrator_module
from services.integration.health_validator import HealthStatus
from services.integration.orchestrator import (
    CATEGORY_WEIGHTS, EXPECTED_AGENT_COUNT, EXPECTED_DEPARTMENTS,
    compute_readiness, generate_full_report, validate_agent_registry,
)
from services.integration.repository import (
    DependencyCheckRepository, PlatformReportRepository, ValidationResultRepository,
)
from services.integration.startup import StartupValidationError, run_startup_checks


# ══════════════════════════════════════════════════════════════
# Shared helpers
# ══════════════════════════════════════════════════════════════

def make_infra():
    inner_db = MagicMock(
        execute=AsyncMock(return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=None),
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))))),
        flush=AsyncMock(), add=MagicMock())
    db = MagicMock()
    db.__aenter__ = AsyncMock(return_value=inner_db)
    db.__aexit__ = AsyncMock(return_value=None)
    return lambda: db, inner_db


def make_factory():
    return AgentFactory(db_factory=None, nats=None, storage=None,
                         audit_repo=None, artifact_repo=None, token_repo=None)


# ══════════════════════════════════════════════════════════════
# LAYER 1a — Unit: dependency_graph
# ══════════════════════════════════════════════════════════════

class TestDependencyGraph:
    def test_phase_tiers_cover_all_pipeline_departments(self):
        flat = dependency_graph.topological_order()
        assert flat == ["product", "architecture", "engineering", "qa", "security",
                         "devops", "monitoring", "incident_response"]

    def test_qa_and_security_share_a_tier(self):
        assert set(dependency_graph.PHASE_TIERS[3]) == {"qa", "security"}

    def test_product_has_no_dependencies(self):
        assert dependency_graph.required_dependencies_for("product") == []

    def test_engineering_depends_on_architecture(self):
        assert dependency_graph.required_dependencies_for("engineering") == ["architecture"]

    def test_devops_depends_on_qa_and_security(self):
        assert set(dependency_graph.required_dependencies_for("devops")) == {"qa", "security"}

    def test_monitoring_depends_on_devops(self):
        assert dependency_graph.required_dependencies_for("monitoring") == ["devops"]

    def test_incident_response_depends_on_monitoring(self):
        assert dependency_graph.required_dependencies_for("incident_response") == ["monitoring"]

    def test_docs_and_manager_are_orphan_departments(self):
        assert dependency_graph.ORPHAN_DEPARTMENTS == {"docs", "manager"}

    def test_has_cycle_is_false_for_canonical_graph(self):
        assert dependency_graph.has_cycle() is False

    def test_validate_dependency_passes_when_satisfied(self):
        r = dependency_graph.validate_dependency("engineering", {"product", "architecture"})
        assert r.passed

    def test_validate_dependency_fails_when_missing(self):
        r = dependency_graph.validate_dependency("devops", {"qa"})
        assert not r.passed
        assert r.missing == ["security"]

    def test_validate_dependency_unknown_department(self):
        r = dependency_graph.validate_dependency("docs", set())
        assert not r.passed
        assert "not a recognized pipeline department" in r.reason

    def test_validate_transition_entry_phase_ok(self):
        r = dependency_graph.validate_transition(None, "product")
        assert r.passed

    def test_validate_transition_entry_phase_rejects_nonempty_predecessor(self):
        r = dependency_graph.validate_transition("architecture", "product")
        assert not r.passed

    def test_validate_transition_valid_adjacent_step(self):
        r = dependency_graph.validate_transition("product", "architecture")
        assert r.passed

    def test_validate_transition_skipped_phase_rejected(self):
        r = dependency_graph.validate_transition("product", "engineering")
        assert not r.passed
        assert "Impossible/skipped" in r.reason

    def test_validate_transition_qa_from_engineering_ok(self):
        assert dependency_graph.validate_transition("engineering", "qa").passed

    def test_validate_transition_security_from_engineering_ok(self):
        assert dependency_graph.validate_transition("engineering", "security").passed

    def test_validate_transition_devops_from_qa_ok(self):
        assert dependency_graph.validate_transition("qa", "devops").passed

    def test_validate_transition_devops_from_security_ok(self):
        assert dependency_graph.validate_transition("security", "devops").passed

    def test_validate_transition_unknown_target(self):
        r = dependency_graph.validate_transition("devops", "docs")
        assert not r.passed

    def test_full_lifecycle_report_covers_all_departments(self):
        report = dependency_graph.full_lifecycle_report(set())
        assert set(report.keys()) == set(dependency_graph.ALL_PIPELINE_DEPARTMENTS)

    def test_full_lifecycle_report_all_pass_when_fully_completed(self):
        report = dependency_graph.full_lifecycle_report(set(dependency_graph.ALL_PIPELINE_DEPARTMENTS))
        assert all(r.passed for r in report.values())


# ══════════════════════════════════════════════════════════════
# LAYER 1b — Unit: artifact_validator
# ══════════════════════════════════════════════════════════════

class TestArtifactValidator:
    def test_engineering_requires_three_documented_artifacts(self):
        required = set(artifact_validator.required_artifacts_for("engineering"))
        assert {"architecture_blueprint", "database_schema", "openapi_spec"} <= required

    def test_qa_requires_source_code(self):
        assert artifact_validator.required_artifacts_for("qa") == ["source_code"]

    def test_security_requires_source_code(self):
        assert artifact_validator.required_artifacts_for("security") == ["source_code"]

    def test_devops_requires_qa_and_security_reports(self):
        required = set(artifact_validator.required_artifacts_for("devops"))
        assert {"qa_report", "security_report"} <= required

    def test_monitoring_has_no_required_artifacts(self):
        assert artifact_validator.required_artifacts_for("monitoring") == []

    def test_incident_response_has_no_required_artifacts(self):
        assert artifact_validator.required_artifacts_for("incident_response") == []

    def test_validate_artifacts_passes_when_available(self):
        r = artifact_validator.validate_artifacts("qa", {"source_code"})
        assert r.passed
        assert r.missing == []

    def test_validate_artifacts_fails_when_missing(self):
        r = artifact_validator.validate_artifacts("devops", {"qa_report"})
        assert not r.passed
        assert "security_report" in r.missing

    def test_validate_artifacts_unknown_department_has_no_requirements(self):
        r = artifact_validator.validate_artifacts("docs", set())
        assert r.passed  # no entry -> empty requirement set -> trivially satisfied

    def test_cross_check_with_manager_manifest_has_no_drift(self):
        """This is the actual regression test for the M3.9 genuine bug
        fix — before the fix, this would report qa/security drift."""
        drift = artifact_validator.cross_check_with_manager_manifest()
        assert drift == {}

    def test_produced_artifacts_engineering_includes_source_code(self):
        assert "source_code" in artifact_validator.PRODUCED_ARTIFACTS["engineering"]

    def test_produced_artifacts_qa_includes_qa_report(self):
        assert "qa_report" in artifact_validator.PRODUCED_ARTIFACTS["qa"]

    @pytest.mark.parametrize("dept", ["product", "architecture", "engineering", "qa", "security", "devops"])
    def test_every_delegated_department_has_a_required_artifacts_entry(self, dept):
        assert dept in artifact_validator.REQUIRED_ARTIFACTS


# ══════════════════════════════════════════════════════════════
# LAYER 1c — Unit: event_router
# ══════════════════════════════════════════════════════════════

class TestEventRouter:
    def test_manager_deploy_approved_is_a_dead_subscription(self):
        dead = event_router.find_dead_subscriptions()
        subjects = {f.subject for f in dead}
        assert "manager.deploy.approved" in subjects

    def test_qa_phase_completed_is_not_orphan(self):
        orphans = {f.subject for f in event_router.find_orphan_events()}
        assert "qa.phase.completed" not in orphans

    def test_engineering_phase_completed_has_duplicate_consumers(self):
        dupes = {f.subject for f in event_router.find_duplicate_consumers()}
        assert "engineering.phase.completed" in dupes

    def test_qa_defect_created_is_a_missing_route(self):
        missing = {f.subject for f in event_router.find_missing_routes()}
        assert "qa.defect.created" in missing

    def test_qa_retry_requested_is_a_missing_route(self):
        missing = {f.subject for f in event_router.find_missing_routes()}
        assert "qa.retry.requested" in missing

    def test_branch_created_is_a_namespace_mismatch(self):
        mismatches = {f.subject for f in event_router.find_namespace_mismatches()}
        assert "branch.created" in mismatches

    def test_repository_created_matches_wildcard(self):
        assert event_router._matches_wildcard("repository.created")

    def test_branch_created_does_not_match_wildcard(self):
        assert not event_router._matches_wildcard("branch.created")

    def test_generate_event_report_totals_are_consistent(self):
        r = event_router.generate_event_report()
        flagged = len(r.orphan_events) + len(r.dead_subscriptions) + len(r.missing_routes) + len(r.namespace_mismatches)
        assert flagged + len(r.healthy_subjects) <= r.total_subjects + len(r.namespace_mismatches)
        # namespace_mismatches overlap with orphan classification by design (both apply to branch.created etc.)

    def test_generate_event_report_finds_at_least_one_of_each_kind(self):
        r = event_router.generate_event_report()
        assert len(r.orphan_events) > 0
        assert len(r.dead_subscriptions) > 0
        assert len(r.missing_routes) > 0
        assert len(r.namespace_mismatches) > 0
        assert len(r.duplicate_consumers) > 0
        assert len(r.healthy_subjects) > 0

    def test_monitoring_incident_is_healthy(self):
        r = event_router.generate_event_report()
        assert "monitoring.incident" in r.healthy_subjects


# ══════════════════════════════════════════════════════════════
# LAYER 1d — Unit: lifecycle (reachability algorithm)
# ══════════════════════════════════════════════════════════════

class TestLifecycleReachability:
    def test_send_target_detected_with_node_name(self):
        src = 'Send("worker_node", state)'
        assert lifecycle._is_likely_send_target("worker", src)

    def test_send_target_not_detected_without_send_call(self):
        src = 'call("worker_node", state)'
        assert not lifecycle._is_likely_send_target("worker", src)

    def test_send_target_not_detected_when_name_absent(self):
        src = 'Send("other_node", state)'
        assert not lifecycle._is_likely_send_target("worker", src)

    def test_bfs_basic_reachability(self):
        adj = {"a": ["b"], "b": ["c"], "c": []}
        assert lifecycle._bfs(adj, "a") == {"a", "b", "c"}

    def test_bfs_no_outgoing_edges(self):
        adj: Dict[str, List[str]] = {}
        assert lifecycle._bfs(adj, "solo") == {"solo"}

    def test_analyze_graph_handles_builder_exception(self):
        def _broken_builder():
            raise RuntimeError("boom")
        result = lifecycle.analyze_graph("broken", _broken_builder)
        assert not result.built
        assert not result.passed
        assert "boom" in result.error

    def test_validate_routing_functions_exist_all_present(self):
        result = lifecycle.validate_routing_functions_exist(
            "services.incident_response.routing",
            ["route_after_intake", "route_after_analyze", "route_after_recover", "route_after_communicate"])
        assert all(result.values())

    def test_validate_routing_functions_exist_missing_one(self):
        result = lifecycle.validate_routing_functions_exist(
            "services.incident_response.routing", ["route_after_intake", "route_does_not_exist"])
        assert result["route_after_intake"] is True
        assert result["route_does_not_exist"] is False

    def test_validate_routing_functions_exist_bad_module(self):
        result = lifecycle.validate_routing_functions_exist("services.nonexistent.module", ["foo"])
        assert result["foo"] is False


# ══════════════════════════════════════════════════════════════
# LAYER 2a — Integration: lifecycle against the real platform
# ══════════════════════════════════════════════════════════════

class TestLifecycleRealPlatform:
    def test_all_ten_graphs_build(self):
        results = lifecycle.validate_all_workflows()
        assert len(results) == 10
        for name, r in results.items():
            assert r.built, f"{name} failed to build: {r.error}"

    def test_qa_dlq_node_is_a_genuine_unreachable_finding(self):
        """Regression test for the real bug discovered during M3.9
        reconnaissance: QA's `dlq` node has no incoming edge anywhere
        in services/qa/workflows/qa_graph.py."""
        results = lifecycle.validate_all_workflows()
        assert not results["qa"].passed
        assert "dlq" in results["qa"].unreachable_nodes

    def test_architecture_parallel_nodes_are_dynamic_not_unreachable(self):
        """Regression test for the Send()-heuristic: these 3 nodes must
        NOT be reported as real failures."""
        results = lifecycle.validate_all_workflows()
        assert results["architecture"].passed
        assert set(results["architecture"].likely_dynamic_dispatch_nodes) == {
            "parallel_integration", "parallel_scaling", "parallel_security"}

    def test_nine_of_ten_graphs_pass(self):
        results = lifecycle.validate_all_workflows()
        passing = sum(1 for r in results.values() if r.passed)
        assert passing == 9  # only qa fails, on the genuine dlq finding

    def test_every_graph_reaches_end(self):
        results = lifecycle.validate_all_workflows()
        for name, r in results.items():
            assert r.end_reachable, f"{name} cannot reach __end__"


# ══════════════════════════════════════════════════════════════
# LAYER 2b — Integration: health_validator
# ══════════════════════════════════════════════════════════════

class TestHealthValidator:
    @pytest.mark.asyncio
    async def test_check_database_failed_without_factory(self):
        result = await health_validator.check_database(None)
        assert result.status == HealthStatus.FAILED

    @pytest.mark.asyncio
    async def test_check_database_healthy_with_working_factory(self):
        db_factory, inner_db = make_infra()
        result = await health_validator.check_database(db_factory)
        assert result.status == HealthStatus.HEALTHY

    @pytest.mark.asyncio
    async def test_check_nats_failed_when_none(self):
        result = await health_validator.check_nats(None)
        assert result.status == HealthStatus.FAILED

    @pytest.mark.asyncio
    async def test_check_nats_healthy_when_present(self):
        result = await health_validator.check_nats(AsyncMock())
        assert result.status == HealthStatus.HEALTHY

    def test_check_department_agent_healthy(self):
        factory = make_factory()
        result = health_validator.check_department_agent("qa", factory)
        assert result.status == HealthStatus.HEALTHY

    def test_check_department_agent_failed_without_factory(self):
        result = health_validator.check_department_agent("qa", None)
        assert result.status == HealthStatus.FAILED

    def test_check_department_agent_unknown_department(self):
        result = health_validator.check_department_agent("nonexistent", make_factory())
        assert result.status == HealthStatus.FAILED

    def test_check_websocket_healthy(self):
        assert health_validator.check_websocket().status == HealthStatus.HEALTHY

    def test_check_telemetry_healthy(self):
        assert health_validator.check_telemetry().status == HealthStatus.HEALTHY

    def test_check_repository_module_healthy(self):
        assert health_validator.check_repository_module().status == HealthStatus.HEALTHY

    def test_overall_status_all_healthy(self):
        components = [health_validator.ComponentHealth(name="a", status=HealthStatus.HEALTHY)] * 5
        assert health_validator.overall_status(components) == HealthStatus.HEALTHY

    def test_overall_status_empty_is_failed(self):
        assert health_validator.overall_status([]) == HealthStatus.FAILED

    def test_overall_status_mostly_healthy_with_one_failed_is_degraded(self):
        components = [health_validator.ComponentHealth(name=f"c{i}", status=HealthStatus.HEALTHY) for i in range(9)]
        components.append(health_validator.ComponentHealth(name="bad", status=HealthStatus.FAILED))
        assert health_validator.overall_status(components) == HealthStatus.DEGRADED

    def test_overall_status_mostly_failed_is_failed(self):
        components = [health_validator.ComponentHealth(name=f"c{i}", status=HealthStatus.FAILED) for i in range(5)]
        assert health_validator.overall_status(components) == HealthStatus.FAILED

    @pytest.mark.asyncio
    async def test_generate_health_report_shape(self):
        factory = make_factory()
        report = await health_validator.generate_health_report(db_factory=None, nats=None, factory=factory)
        names = {c.name for c in report.components}
        assert set(health_validator.COMPONENTS) <= names

    def test_validate_repository_layer_no_gaps_when_all_present(self):
        all_tables = [t for group in health_validator.EXPECTED_TABLES.values() for t in group]
        gaps = health_validator.validate_repository_layer(all_tables)
        assert gaps == {}

    def test_validate_repository_layer_reports_gaps(self):
        gaps = health_validator.validate_repository_layer([])
        assert set(gaps.keys()) == set(health_validator.EXPECTED_TABLES.keys())


# ══════════════════════════════════════════════════════════════
# LAYER 2c — Integration: orchestrator
# ══════════════════════════════════════════════════════════════

class TestOrchestratorRegistry:
    def test_validate_agent_registry_passes_on_real_platform(self):
        r = validate_agent_registry()
        assert r.passed
        assert r.total_agents == EXPECTED_AGENT_COUNT

    def test_validate_agent_registry_covers_all_departments(self):
        r = validate_agent_registry()
        assert set(r.departments) == EXPECTED_DEPARTMENTS

    def test_manager_agent_has_no_parent(self):
        assert AGENT_REGISTRY["manager_agent"].parent_agent_id is None

    def test_incident_response_head_chain_terminates_at_manager(self):
        r = validate_agent_registry()
        assert not any(f.kind == "broken_parent_chain" and "incident_response_head" in f.detail
                       for f in r.findings)


class TestOrchestratorReadiness:
    @pytest.mark.asyncio
    async def test_compute_readiness_weights_sum_to_one(self):
        assert abs(sum(CATEGORY_WEIGHTS.values()) - 1.0) < 1e-9

    @pytest.mark.asyncio
    async def test_generate_full_report_shape(self):
        factory = make_factory()
        full = await generate_full_report(factory=factory)
        assert 0.0 <= full.readiness.overall <= 100.0
        assert len(full.readiness.categories) == 8

    @pytest.mark.asyncio
    async def test_generate_full_report_registry_passes(self):
        factory = make_factory()
        full = await generate_full_report(factory=factory)
        assert full.registry.passed

    @pytest.mark.asyncio
    async def test_generate_full_report_workflows_has_ten_entries(self):
        factory = make_factory()
        full = await generate_full_report(factory=factory)
        assert len(full.workflows) == 10

    def test_compute_readiness_perfect_inputs_yields_100(self):
        from services.integration.health_validator import ComponentHealth, PlatformHealthReport
        from services.integration.orchestrator import RegistryValidationReport
        from services.integration.event_router import EventValidationReport
        from services.integration.lifecycle import GraphAnalysis

        health = PlatformHealthReport(overall=HealthStatus.HEALTHY, components=[
            ComponentHealth(name="x", status=HealthStatus.HEALTHY),
            ComponentHealth(name="monitoring", status=HealthStatus.HEALTHY),
            ComponentHealth(name="incident_response", status=HealthStatus.HEALTHY),
        ])
        registry = RegistryValidationReport(total_agents=106, passed=True, departments=[])
        events = EventValidationReport(total_subjects=10, healthy_subjects=[f"s{i}" for i in range(10)])
        workflows = {"a": GraphAnalysis(name="a", built=True, passed=True)}
        readiness = compute_readiness(health, registry, events, workflows, {}, documentation_score=100.0)
        assert readiness.overall == 100.0

    def test_compute_readiness_degrades_with_problems(self):
        from services.integration.health_validator import ComponentHealth, PlatformHealthReport
        from services.integration.orchestrator import RegistryValidationReport
        from services.integration.event_router import EventValidationReport, EventFinding
        from services.integration.lifecycle import GraphAnalysis

        health = PlatformHealthReport(overall=HealthStatus.DEGRADED, components=[
            ComponentHealth(name="x", status=HealthStatus.FAILED)])
        registry = RegistryValidationReport(total_agents=106, passed=False,
                                             findings=[orchestrator_module.RegistryFinding(
                                                 kind="count_mismatch", detail="x")])
        events = EventValidationReport(total_subjects=10,
                                         orphan_events=[EventFinding(subject="s1", kind="orphan", detail="d")])
        workflows = {"a": GraphAnalysis(name="a", built=True, passed=False)}
        readiness = compute_readiness(health, registry, events, workflows, {"g": ["t1"]}, documentation_score=50.0)
        assert readiness.overall < 100.0


# ══════════════════════════════════════════════════════════════
# LAYER 2d — Integration: startup
# ══════════════════════════════════════════════════════════════

class TestStartup:
    @pytest.mark.asyncio
    async def test_run_startup_checks_non_strict_returns_report(self):
        report = await run_startup_checks(db_factory=None, strict=False)
        assert isinstance(report.passed, bool)

    @pytest.mark.asyncio
    async def test_run_startup_checks_strict_raises_without_db(self):
        with pytest.raises(StartupValidationError):
            await run_startup_checks(db_factory=None, strict=True)

    @pytest.mark.asyncio
    async def test_settings_check_passes(self):
        from services.integration.startup import _check_settings
        assert _check_settings().passed

    @pytest.mark.asyncio
    async def test_ports_check_passes_no_collisions(self):
        from services.integration.startup import _check_ports
        result = _check_ports()
        assert result.passed

    @pytest.mark.asyncio
    async def test_agent_registry_check_passes(self):
        from services.integration.startup import _check_agent_registry
        assert _check_agent_registry().passed

    @pytest.mark.asyncio
    async def test_workflow_registration_check_passes(self):
        from services.integration.startup import _check_workflow_registration
        assert _check_workflow_registration().passed

    @pytest.mark.asyncio
    async def test_nats_subjects_check_passes(self):
        from services.integration.startup import _check_nats_subjects
        assert _check_nats_subjects().passed

    @pytest.mark.asyncio
    async def test_artifact_registry_check_passes(self):
        from services.integration.startup import _check_artifact_registry
        assert _check_artifact_registry().passed

    @pytest.mark.asyncio
    async def test_migrations_check_fails_without_db(self):
        from services.integration.startup import _check_migrations
        result = await _check_migrations(None)
        assert not result.passed

    @pytest.mark.asyncio
    async def test_repository_connectivity_check_fails_without_db(self):
        from services.integration.startup import _check_repository_connectivity
        result = await _check_repository_connectivity(None)
        assert not result.passed


# ══════════════════════════════════════════════════════════════
# LAYER 2e — Integration: repository classes (mocked DB)
# ══════════════════════════════════════════════════════════════

class TestPlatformReportRepository:
    @pytest.mark.asyncio
    async def test_record_returns_row(self):
        db_factory, inner_db = make_infra()
        async with db_factory() as db:
            row = await PlatformReportRepository.record(db, 98.4, {"registry": 100.0}, "healthy")
            assert row.readiness_overall == 98.4

    @pytest.mark.asyncio
    async def test_latest_returns_none_when_empty(self):
        db_factory, inner_db = make_infra()
        async with db_factory() as db:
            row = await PlatformReportRepository.latest(db)
            assert row is None

    @pytest.mark.asyncio
    async def test_list_recent_returns_list(self):
        db_factory, inner_db = make_infra()
        async with db_factory() as db:
            rows = await PlatformReportRepository.list_recent(db)
            assert rows == []


class TestValidationResultRepository:
    @pytest.mark.asyncio
    async def test_record_returns_row(self):
        db_factory, inner_db = make_infra()
        async with db_factory() as db:
            row = await ValidationResultRepository.record(db, "report-1", "registry", True, {})
            assert row.category == "registry"


class TestDependencyCheckRepository:
    @pytest.mark.asyncio
    async def test_record_returns_row(self):
        db_factory, inner_db = make_infra()
        async with db_factory() as db:
            row = await DependencyCheckRepository.record(db, "report-1", "qa", True, {})
            assert row.department == "qa"


# ══════════════════════════════════════════════════════════════
# LAYER 3 — API routes
# ══════════════════════════════════════════════════════════════

def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from services.integration.api.routes import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestAPIRoutes:
    def test_health_endpoint(self):
        resp = _client().get("/platform/health")
        assert resp.status_code == 200
        assert "overall" in resp.json()

    def test_readiness_endpoint(self):
        resp = _client().get("/platform/readiness")
        assert resp.status_code == 200
        assert "overall" in resp.json()

    def test_dependencies_endpoint(self):
        resp = _client().get("/platform/dependencies")
        assert resp.status_code == 200
        assert "phase_tiers" in resp.json()

    def test_events_endpoint(self):
        resp = _client().get("/platform/events")
        assert resp.status_code == 200
        assert "orphan_events" in resp.json()

    def test_workflows_endpoint(self):
        resp = _client().get("/platform/workflows")
        assert resp.status_code == 200
        assert "qa" in resp.json()

    def test_registry_endpoint(self):
        resp = _client().get("/platform/registry")
        assert resp.status_code == 200
        assert resp.json()["total_agents"] == 106

    def test_report_endpoint(self):
        resp = _client().get("/platform/report")
        assert resp.status_code == 200
        body = resp.json()
        assert "readiness" in body
        assert "health" in body
        assert "workflows" in body


# ══════════════════════════════════════════════════════════════
# LAYER 4 — E2E: full platform validation run
# ══════════════════════════════════════════════════════════════

class TestFullPlatformValidation:
    @pytest.mark.asyncio
    async def test_full_report_against_real_platform(self):
        factory = make_factory()
        full = await generate_full_report(factory=factory)

        assert full.registry.total_agents == 106
        assert full.registry.passed
        assert len(full.workflows) == 10
        assert sum(1 for w in full.workflows.values() if w.passed) == 9
        assert full.events.total_subjects > 40
        assert full.readiness.overall > 0

    @pytest.mark.asyncio
    async def test_readiness_reflects_genuine_findings_not_perfect_score(self):
        """The platform has real, documented findings (QA's dlq node,
        several orphan/dead NATS subjects) — a perfectly clean 100%
        readiness score would indicate this validator isn't actually
        checking anything."""
        factory = make_factory()
        full = await generate_full_report(factory=factory)
        assert full.readiness.overall < 100.0

    @pytest.mark.asyncio
    async def test_registry_and_workflows_categories_reflect_real_state(self):
        factory = make_factory()
        full = await generate_full_report(factory=factory)
        categories = {c.name: c.score for c in full.readiness.categories}
        assert categories["registry"] == 100.0  # registry itself is clean
        assert categories["workflows"] == 90.0   # 9/10 graphs pass
