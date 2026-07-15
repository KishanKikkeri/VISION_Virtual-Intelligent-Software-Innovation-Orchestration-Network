"""
tests/foundation/test_m34_qa.py
==================================
M3.4 QA Service tests — 4 layers matching the M3.1/M3.2/M3.3 pattern.

Layer 1 — Unit:        models, utils, task decomposition, validation-gate
                       logic (classify_defects/build_qa_report), routing
                       predicates, agent registry verification
Layer 2 — Graph:       LangGraph node functions + graph construction
Layer 3 — Integration: workers (mocked LLM), read-only Repository Service
                       client (mocked httpx), leads (fake factory)
Layer 4 — E2E:         full QAHead pipeline (fake factory chain)
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.contracts import AgentResult, TaskStatus
from core.runtime.context import AgentContext, TaskInput
from core.runtime.factory import AGENT_REGISTRY

from services.qa.models import (
    CompatibilityReport,
    CoverageReport,
    DefectReport,
    DefectSeverity,
    FailureCategory,
    PerformanceReport,
    QAPlan,
    QAReport,
    QATask,
    QATaskStatus,
    QATeam,
    QAVerdict,
    RegressionReport,
    RetryRequest,
    SuiteType,
    TestFile,
    TestSuite,
)
from services.qa.utils import (
    defect_id_for,
    exponential_backoff_seconds,
    files_to_dicts,
    idempotency_key,
    parse_llm_json,
    quality_gate,
    severity_for_category,
    summarize_failures,
)
from services.qa.context import (
    DEFAULT_COVERAGE_THRESHOLD,
    INTEGRATION_WORKERS,
    PERFORMANCE_WORKERS,
    REGRESSION_WORKERS,
    UNIT_WORKERS,
    build_qa_plan,
    build_qa_report,
    build_retry_request,
    classify_defects,
    team_progress,
    topological_batches,
)
from services.qa.routing import (
    MAX_RETRY_CYCLES,
    MAX_TASK_RETRIES,
    route_after_aggregate,
    route_after_coverage,
    route_after_defect_report,
    route_after_execute,
    route_after_generate,
    route_after_validate_inputs,
    route_checkpoint_recovery,
    route_task_retry,
)
from services.qa.integration.repository_client import (
    QARepositoryReadClient,
    RepositoryServiceClientError,
)
from services.qa.schemas import QAServiceError


# ══════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════

@pytest.fixture
def project_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def qa_context(project_id) -> AgentContext:
    return AgentContext(
        project_id=project_id, workflow_id=str(uuid.uuid4()),
        current_phase=5, project_name="TestApp",
        project_description="A test SaaS app.",
        approved_artifacts={
            "source_code": {"files": [{"path": "app/main.py", "language": "python", "content": "x"},
                                       {"path": "app/auth.py", "language": "python", "content": "y"}]},
            "openapi_spec": {"paths": {"/health": {}, "/auth/login": {}, "/users": {}}},
            "database_schema": {"tables": [{"name": "users"}]},
            "implementation_plan": {"plan_id": "plan-1"},
            "engineering_review_report": {"reviewed": 3},
        },
        tech_stack={"backend": "Python+FastAPI", "frontend": "React+TS"},
        llm_provider="anthropic", llm_model="claude-sonnet-4-6",
        budget_limit_usd=50.0, total_spend_usd=2.0,
    )


@pytest.fixture
def qa_context_missing_inputs(qa_context) -> AgentContext:
    qa_context.approved_artifacts = {
        k: v for k, v in qa_context.approved_artifacts.items() if k != "source_code"
    }
    return qa_context


@pytest.fixture
def qa_task(project_id, qa_context) -> TaskInput:
    return TaskInput(
        task_id=str(uuid.uuid4()), project_id=project_id,
        agent_id="qa_head", parent_agent_id="manager_agent",
        task_type="run_qa_pipeline",
        description="Validate approved Engineering output",
        expected_output="QAReport with verdict pass/warn/fail",
        context=qa_context,
    )


@pytest.fixture
def qa_task_missing_inputs(project_id, qa_context_missing_inputs) -> TaskInput:
    return TaskInput(
        task_id=str(uuid.uuid4()), project_id=project_id,
        agent_id="qa_head", parent_agent_id="manager_agent",
        task_type="run_qa_pipeline",
        description="Validate approved Engineering output (missing inputs)",
        expected_output="QAReport with verdict pass/warn/fail",
        context=qa_context_missing_inputs,
    )


def make_infra():
    db = MagicMock()
    db.__aenter__ = AsyncMock(return_value=MagicMock(
        execute=AsyncMock(return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=None),
            scalar_one=MagicMock(return_value=0))),
        flush=AsyncMock(), add=MagicMock()))
    db.__aexit__ = AsyncMock(return_value=None)
    storage = AsyncMock()
    storage.store = AsyncMock(return_value="local://test/v1.json")

    async def _create_artifact(db, project_id, artifact_type, created_by,
                                content=None, storage_ref=None, metadata=None):
        return {"artifact_id": str(uuid.uuid4()), "artifact_type": artifact_type,
                "version": 1, "storage_ref": storage_ref or "local://test/v1.json"}

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
    a.department = "qa"
    a.layer = layer
    a.role = role
    a.responsibilities = ["QA validation"]
    a._db_factory = infra["db_factory"]; a._nats = infra["nats"]; a._storage = infra["storage"]
    a._audit_repo = infra["audit_repo"]; a._artifact_repo = infra["artifact_repo"]
    a._token_repo = infra["token_repo"]; a._qdrant = None
    return a


def patched(agent, raw_response, usage=None):
    return (
        patch.object(agent, "call_llm", AsyncMock(return_value=(raw_response, usage))),
        patch.object(agent, "_pre_execute", AsyncMock()),
        patch.object(agent, "_post_execute", AsyncMock()),
    )


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
        if callable(val) and not isinstance(val, AgentResult):
            return _CallableAgent(agent_id, val)
        return FakeAgent(agent_id, val)


class _CallableAgent:
    def __init__(self, agent_id, fn):
        self.agent_id = agent_id
        self._fn = fn

    async def run(self, task: TaskInput) -> AgentResult:
        return self._fn(task)


def ok_result(agent_id: str, **content) -> AgentResult:
    return AgentResult(task_id="t", agent_id=agent_id, status=TaskStatus.COMPLETED,
                        content=content or {"suite_id": agent_id, "test_count": 5},
                        quality_score=0.9,
                        artifacts=[{"artifact_id": "a1", "artifact_type": content.get("__artifact_type__", "unit_test_suite"), "version": 1}])


def fail_result(agent_id: str, reason: str) -> AgentResult:
    return AgentResult(task_id="t", agent_id=agent_id, status=TaskStatus.FAILED,
                        content={}, quality_score=0.0, failure_reason=reason)


# ══════════════════════════════════════════════════════════════
# LAYER 1a — Unit: models
# ══════════════════════════════════════════════════════════════

class TestTestSuite:
    def test_is_executable_true_with_files_and_count(self):
        s = TestSuite(project_id="p", task_id="t", suite_type=SuiteType.UNIT,
                       files=[TestFile(path="a.py", language="python", content="x")],
                       test_count=5, generated_by="w")
        assert s.is_executable is True

    def test_is_executable_false_without_files(self):
        s = TestSuite(project_id="p", task_id="t", suite_type=SuiteType.UNIT,
                       files=[], test_count=5, generated_by="w")
        assert s.is_executable is False

    def test_is_executable_false_with_zero_test_count(self):
        s = TestSuite(project_id="p", task_id="t", suite_type=SuiteType.UNIT,
                       files=[TestFile(path="a.py", language="python", content="x")],
                       test_count=0, generated_by="w")
        assert s.is_executable is False

    def test_contract_no_violations_when_complete(self):
        s = TestSuite(project_id="p", task_id="t", suite_type=SuiteType.UNIT,
                       files=[TestFile(path="a.py", language="python", content="x")],
                       test_count=5, quality_score=0.9, generated_by="w", idempotent_key="abc")
        assert s.satisfies_test_contract() == []

    def test_contract_flags_missing_files(self):
        s = TestSuite(project_id="p", task_id="t", suite_type=SuiteType.UNIT,
                       files=[], test_count=0, generated_by="w", idempotent_key="abc")
        assert "executable" in s.satisfies_test_contract()

    def test_contract_flags_zero_tests(self):
        s = TestSuite(project_id="p", task_id="t", suite_type=SuiteType.UNIT,
                       files=[TestFile(path="a.py", language="python", content="x")],
                       test_count=0, quality_score=0.9, generated_by="w", idempotent_key="abc")
        assert "non_empty" in s.satisfies_test_contract()

    def test_contract_flags_low_score(self):
        s = TestSuite(project_id="p", task_id="t", suite_type=SuiteType.UNIT,
                       files=[TestFile(path="a.py", language="python", content="x")],
                       test_count=5, quality_score=0.5, generated_by="w", idempotent_key="abc")
        assert "reviewable" in s.satisfies_test_contract()

    def test_contract_flags_missing_idempotent_key(self):
        s = TestSuite(project_id="p", task_id="t", suite_type=SuiteType.UNIT,
                       files=[TestFile(path="a.py", language="python", content="x")],
                       test_count=5, quality_score=0.9, generated_by="w", idempotent_key=None)
        assert "idempotent" in s.satisfies_test_contract()

    def test_suite_type_enum_covers_all_teams(self):
        assert {t.value for t in SuiteType} == {"unit", "integration", "regression", "performance"}


class TestQATask:
    def test_can_run_with_no_dependencies(self):
        t = QATask(project_id="p", team=QATeam.UNIT, worker_agent_id="w")
        assert t.can_run(set()) is True

    def test_can_run_false_when_dependency_incomplete(self):
        t = QATask(project_id="p", team=QATeam.UNIT, worker_agent_id="w", depends_on=["dep-1"])
        assert t.can_run(set()) is False

    def test_can_run_true_when_dependency_complete(self):
        t = QATask(project_id="p", team=QATeam.UNIT, worker_agent_id="w", depends_on=["dep-1"])
        assert t.can_run({"dep-1"}) is True

    def test_can_run_false_when_not_pending(self):
        t = QATask(project_id="p", team=QATeam.UNIT, worker_agent_id="w",
                    status=QATaskStatus.RUNNING)
        assert t.can_run(set()) is False

    @pytest.mark.parametrize("retries,expected", [(0, 1), (1, 2), (2, 4), (3, 8), (10, 60)])
    def test_backoff_seconds_exponential_capped(self, retries, expected):
        t = QATask(project_id="p", team=QATeam.UNIT, worker_agent_id="w", retry_count=retries)
        assert t.next_backoff_seconds() == expected


class TestQAPlan:
    def _plan(self):
        t1 = QATask(project_id="p", team=QATeam.UNIT, worker_agent_id="unit_test_writer_worker")
        t2 = QATask(project_id="p", team=QATeam.UNIT, worker_agent_id="coverage_analyzer_worker",
                    depends_on=[t1.task_id])
        t3 = QATask(project_id="p", team=QATeam.INTEGRATION, worker_agent_id="integration_test_writer_worker",
                    status=QATaskStatus.COMPLETED)
        return QAPlan(project_id="p", feature_name="f", tasks=[t1, t2, t3]), t1, t2, t3

    def test_ready_tasks_returns_only_unblocked(self):
        plan, t1, t2, t3 = self._plan()
        ready = plan.ready_tasks(set())
        assert t1 in ready and t2 not in ready

    def test_ready_tasks_after_dependency_completed(self):
        plan, t1, t2, t3 = self._plan()
        ready = plan.ready_tasks({t1.task_id})
        assert t2 in ready

    def test_tasks_by_team_filters_correctly(self):
        plan, t1, t2, t3 = self._plan()
        assert plan.tasks_by_team(QATeam.UNIT) == [t1, t2]
        assert plan.tasks_by_team(QATeam.INTEGRATION) == [t3]

    def test_all_complete_false_when_pending_tasks_remain(self):
        plan, *_ = self._plan()
        assert plan.all_complete is False

    def test_all_complete_true_when_all_completed(self):
        plan, t1, t2, t3 = self._plan()
        t1.status = QATaskStatus.COMPLETED
        t2.status = QATaskStatus.COMPLETED
        assert plan.all_complete is True

    def test_any_dead_lettered_false_by_default(self):
        plan, *_ = self._plan()
        assert plan.any_dead_lettered is False

    def test_any_dead_lettered_true_when_flagged(self):
        plan, t1, t2, t3 = self._plan()
        t2.dead_lettered = True
        assert plan.any_dead_lettered is True


class TestReports:
    def test_coverage_report_build_meets_threshold(self):
        r = CoverageReport.build("p", 85.0)
        assert r.meets_threshold is True
        assert r.line_coverage == 85.0

    def test_coverage_report_build_below_threshold(self):
        r = CoverageReport.build("p", 60.0)
        assert r.meets_threshold is False

    def test_coverage_report_clamps_range(self):
        r = CoverageReport.build("p", 150.0)
        assert r.line_coverage == 100.0
        r2 = CoverageReport.build("p", -10.0)
        assert r2.line_coverage == 0.0

    def test_coverage_report_branch_and_function_derived(self):
        r = CoverageReport.build("p", 80.0)
        assert r.branch_coverage == 75.0
        assert r.function_coverage == 82.0

    def test_regression_report_passed_true_when_no_failures(self):
        r = RegressionReport(project_id="p", tests_run=10, tests_passed=10, tests_failed=0)
        assert r.passed is True

    def test_regression_report_passed_false_with_failures(self):
        r = RegressionReport(project_id="p", tests_run=10, tests_passed=8, tests_failed=2,
                              regressions_detected=["test_x"])
        assert r.passed is False

    def test_performance_report_passes_threshold(self):
        r = PerformanceReport(project_id="p", p95_ms=300, error_rate_pct=0.1)
        assert r.passes_threshold is True

    def test_performance_report_fails_on_high_p95(self):
        r = PerformanceReport(project_id="p", p95_ms=600, error_rate_pct=0.1)
        assert r.passes_threshold is False

    def test_performance_report_fails_on_high_error_rate(self):
        r = PerformanceReport(project_id="p", p95_ms=300, error_rate_pct=6.0)
        assert r.passes_threshold is False

    def test_compatibility_report_passed_by_default(self):
        r = CompatibilityReport(project_id="p")
        assert r.passed is True

    def test_compatibility_report_fails_with_incompatibilities(self):
        r = CompatibilityReport(project_id="p", incompatibilities=["py3.9 unsupported"])
        assert r.passed is False


class TestDefectAndRetry:
    def test_defect_is_blocking_for_critical(self):
        d = DefectReport(project_id="p", severity=DefectSeverity.CRITICAL,
                          category=FailureCategory.BUILD_FAILURE, description="x")
        assert d.is_blocking is True

    def test_defect_is_blocking_for_high(self):
        d = DefectReport(project_id="p", severity=DefectSeverity.HIGH,
                          category=FailureCategory.CONTRACT_BREAK, description="x")
        assert d.is_blocking is True

    def test_defect_not_blocking_for_medium(self):
        d = DefectReport(project_id="p", severity=DefectSeverity.MEDIUM,
                          category=FailureCategory.TEST_FAILURE, description="x")
        assert d.is_blocking is False

    def test_defect_not_blocking_for_low(self):
        d = DefectReport(project_id="p", severity=DefectSeverity.LOW,
                          category=FailureCategory.TEST_FAILURE, description="x")
        assert d.is_blocking is False

    def test_retry_request_can_retry_true_below_max(self):
        r = RetryRequest(project_id="p", target_team="engineering", reason="x", retry_count=1, max_retries=3)
        assert r.can_retry is True

    def test_retry_request_can_retry_false_at_max(self):
        r = RetryRequest(project_id="p", target_team="engineering", reason="x", retry_count=3, max_retries=3)
        assert r.can_retry is False


# ══════════════════════════════════════════════════════════════
# LAYER 1b — Unit: utils
# ══════════════════════════════════════════════════════════════

class TestParseLlmJson:
    def test_parses_plain_json(self):
        assert parse_llm_json('{"a": 1}') == {"a": 1}

    def test_strips_markdown_fences(self):
        assert parse_llm_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_strips_bare_fences(self):
        assert parse_llm_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_returns_fallback_on_invalid_json(self):
        assert parse_llm_json("not json", {"x": 1}) == {"x": 1}

    def test_returns_empty_dict_when_no_fallback_given(self):
        assert parse_llm_json("not json") == {}


class TestIdempotencyKey:
    def test_deterministic_for_same_inputs(self):
        k1 = idempotency_key("p", "t", "w")
        k2 = idempotency_key("p", "t", "w")
        assert k1 == k2

    def test_differs_for_different_inputs(self):
        assert idempotency_key("p", "t", "w1") != idempotency_key("p", "t", "w2")

    def test_key_length(self):
        assert len(idempotency_key("p", "t", "w")) == 16


class TestDefectIdFor:
    def test_deterministic(self):
        assert defect_id_for("p", "build_failure", "m1") == defect_id_for("p", "build_failure", "m1")

    def test_differs_by_category(self):
        assert defect_id_for("p", "build_failure", "m1") != defect_id_for("p", "coverage_gap", "m1")

    def test_handles_missing_module_id(self):
        assert defect_id_for("p", "build_failure", None)


class TestQualityGate:
    def test_passes_at_threshold(self):
        assert quality_gate(0.7) is True

    def test_fails_below_threshold(self):
        assert quality_gate(0.6) is False

    def test_custom_threshold(self):
        assert quality_gate(0.5, threshold=0.4) is True


class TestExponentialBackoff:
    @pytest.mark.parametrize("retries,expected", [(0, 1), (1, 2), (2, 4), (6, 60)])
    def test_backoff_values(self, retries, expected):
        assert exponential_backoff_seconds(retries) == expected


class TestFilesToDicts:
    def test_converts_dict_items(self):
        out = files_to_dicts([{"path": "a.py", "content": "x"}])
        assert out == [{"path": "a.py", "content": "x"}]

    def test_converts_model_items(self):
        f = TestFile(path="a.py", language="python", content="x")
        out = files_to_dicts([f])
        assert out[0]["path"] == "a.py" and out[0]["content"] == "x"


class TestSummarizeFailures:
    def test_joins_reasons(self):
        items = [{"failure_reason": "a"}, {"failure_reason": "b"}]
        assert summarize_failures(items) == "a; b"

    def test_returns_default_when_no_reasons(self):
        assert summarize_failures([{}]) == "no failure detail available"

    def test_caps_at_five(self):
        items = [{"failure_reason": str(i)} for i in range(10)]
        assert summarize_failures(items).count(";") == 4


class TestSeverityForCategory:
    @pytest.mark.parametrize("category,expected", [
        ("build_failure", "critical"),
        ("migration_failure", "critical"),
        ("contract_break", "high"),
        ("coverage_gap", "high"),
        ("regression", "high"),
        ("test_infrastructure_failure", "critical"),
        ("performance", "medium"),
        ("unknown_category", "medium"),
    ])
    def test_severity_mapping(self, category, expected):
        assert severity_for_category(category) == expected


# ══════════════════════════════════════════════════════════════
# LAYER 1c — Unit: task decomposition + validation-gate logic
# ══════════════════════════════════════════════════════════════

class TestBuildQAPlan:
    def test_includes_all_four_teams(self, project_id):
        plan = build_qa_plan(project_id, "f", {"source_code": {}})
        teams = {t.team for t in plan.tasks}
        assert teams == {QATeam.UNIT, QATeam.INTEGRATION, QATeam.REGRESSION, QATeam.PERFORMANCE}

    def test_worker_count_matches_registry(self, project_id):
        plan = build_qa_plan(project_id, "f", {})
        expected = len(UNIT_WORKERS) + len(INTEGRATION_WORKERS) + len(REGRESSION_WORKERS) + len(PERFORMANCE_WORKERS)
        assert len(plan.tasks) == expected

    def test_coverage_analyzer_depends_on_unit_writer(self, project_id):
        plan = build_qa_plan(project_id, "f", {})
        by_worker = {t.worker_agent_id: t for t in plan.tasks}
        cov = by_worker["coverage_analyzer_worker"]
        writer = by_worker["unit_test_writer_worker"]
        assert writer.task_id in cov.depends_on

    def test_plan_stores_engineering_refs(self, project_id):
        refs = {"source_code": {"files": []}}
        plan = build_qa_plan(project_id, "f", refs)
        assert plan.engineering_refs == refs

    def test_plan_has_unique_plan_id(self, project_id):
        p1 = build_qa_plan(project_id, "f", {})
        p2 = build_qa_plan(project_id, "f", {})
        assert p1.plan_id != p2.plan_id


class TestTopologicalBatches:
    def test_independent_tasks_in_one_batch(self):
        tasks = [QATask(project_id="p", team=QATeam.UNIT, worker_agent_id=f"w{i}") for i in range(3)]
        batches = topological_batches(tasks)
        assert len(batches) == 1 and len(batches[0]) == 3

    def test_dependent_tasks_split_into_batches(self):
        t1 = QATask(project_id="p", team=QATeam.UNIT, worker_agent_id="w1")
        t2 = QATask(project_id="p", team=QATeam.UNIT, worker_agent_id="w2", depends_on=[t1.task_id])
        batches = topological_batches([t1, t2])
        assert len(batches) == 2
        assert batches[0] == [t1] and batches[1] == [t2]

    def test_cycle_raises_value_error(self):
        t1 = QATask(project_id="p", team=QATeam.UNIT, worker_agent_id="w1")
        t2 = QATask(project_id="p", team=QATeam.UNIT, worker_agent_id="w2", depends_on=[t1.task_id])
        t1.depends_on = [t2.task_id]
        with pytest.raises(ValueError):
            topological_batches([t1, t2])

    def test_real_plan_batches_correctly(self, project_id):
        plan = build_qa_plan(project_id, "f", {})
        batches = topological_batches(plan.tasks)
        assert sum(len(b) for b in batches) == len(plan.tasks)


class TestTeamProgress:
    def test_progress_counts(self, project_id):
        plan = build_qa_plan(project_id, "f", {})
        plan.tasks[0].status = QATaskStatus.COMPLETED
        progress = team_progress(plan, plan.tasks[0].team)
        assert progress["completed"] >= 1
        assert progress["total"] >= 1


class TestClassifyDefects:
    def _passing_reports(self, project_id):
        return dict(
            coverage=CoverageReport.build(project_id, 90.0),
            regression=RegressionReport(project_id=project_id, tests_run=10, tests_passed=10, tests_failed=0),
            performance=PerformanceReport(project_id=project_id, p95_ms=300, error_rate_pct=0.1),
            compatibility=CompatibilityReport(project_id=project_id),
        )

    def test_no_defects_when_everything_passes(self, project_id):
        defects = classify_defects(project_id, True, True, True, **self._passing_reports(project_id))
        assert defects == []

    def test_build_failure_produces_critical_defect(self, project_id):
        defects = classify_defects(project_id, False, True, True, **self._passing_reports(project_id))
        assert any(d.category == FailureCategory.BUILD_FAILURE and d.severity == DefectSeverity.CRITICAL
                   for d in defects)

    def test_migration_failure_produces_critical_defect(self, project_id):
        defects = classify_defects(project_id, True, False, True, **self._passing_reports(project_id))
        assert any(d.category == FailureCategory.MIGRATION_FAILURE for d in defects)

    def test_contract_break_produces_high_defect(self, project_id):
        defects = classify_defects(project_id, True, True, False, **self._passing_reports(project_id))
        assert any(d.category == FailureCategory.CONTRACT_BREAK and d.severity == DefectSeverity.HIGH
                   for d in defects)

    def test_coverage_gap_produces_defect(self, project_id):
        reports = self._passing_reports(project_id)
        reports["coverage"] = CoverageReport.build(project_id, 50.0)
        defects = classify_defects(project_id, True, True, True, **reports)
        assert any(d.category == FailureCategory.COVERAGE_GAP for d in defects)

    def test_regression_produces_defect(self, project_id):
        reports = self._passing_reports(project_id)
        reports["regression"] = RegressionReport(project_id=project_id, tests_run=10, tests_passed=8,
                                                   tests_failed=2, regressions_detected=["test_x"])
        defects = classify_defects(project_id, True, True, True, **reports)
        assert any(d.category == FailureCategory.REGRESSION for d in defects)

    def test_performance_regression_produces_warning_grade_defect(self, project_id):
        reports = self._passing_reports(project_id)
        reports["performance"] = PerformanceReport(project_id=project_id, p95_ms=600, error_rate_pct=0.1)
        defects = classify_defects(project_id, True, True, True, **reports)
        perf_defects = [d for d in defects if d.category == FailureCategory.PERFORMANCE]
        assert perf_defects and perf_defects[0].is_blocking is False

    def test_compatibility_issue_produces_defect(self, project_id):
        reports = self._passing_reports(project_id)
        reports["compatibility"] = CompatibilityReport(project_id=project_id, incompatibilities=["py3.9"])
        defects = classify_defects(project_id, True, True, True, **reports)
        assert any(d.category == FailureCategory.TEST_FAILURE for d in defects)

    def test_multiple_failures_produce_multiple_defects(self, project_id):
        reports = self._passing_reports(project_id)
        reports["coverage"] = CoverageReport.build(project_id, 40.0)
        defects = classify_defects(project_id, False, False, True, **reports)
        categories = {d.category for d in defects}
        assert FailureCategory.BUILD_FAILURE in categories
        assert FailureCategory.MIGRATION_FAILURE in categories
        assert FailureCategory.COVERAGE_GAP in categories

    def test_defect_ids_deterministic_across_calls(self, project_id):
        reports = self._passing_reports(project_id)
        d1 = classify_defects(project_id, False, True, True, **reports)
        d2 = classify_defects(project_id, False, True, True, **reports)
        assert d1[0].defect_id == d2[0].defect_id


class TestBuildQAReport:
    def _passing_reports(self, project_id):
        return dict(
            coverage=CoverageReport.build(project_id, 90.0),
            regression=RegressionReport(project_id=project_id, tests_run=10, tests_passed=10, tests_failed=0),
            performance=PerformanceReport(project_id=project_id, p95_ms=300, error_rate_pct=0.1),
            compatibility=CompatibilityReport(project_id=project_id),
        )

    def test_verdict_pass_with_no_defects(self, project_id):
        reports = self._passing_reports(project_id)
        report = build_qa_report(project_id, True, True, True, **reports,
                                  tests_total=10, tests_passed=10, tests_failed=0, defects=[])
        assert report.verdict == QAVerdict.PASS
        assert report.retry_requested is False

    def test_verdict_fail_with_blocking_defect(self, project_id):
        reports = self._passing_reports(project_id)
        defects = classify_defects(project_id, False, True, True, **reports)
        report = build_qa_report(project_id, False, True, True, **reports,
                                  tests_total=10, tests_passed=8, tests_failed=2, defects=defects)
        assert report.verdict == QAVerdict.FAIL
        assert report.retry_requested is True

    def test_verdict_warn_with_only_nonblocking_defects(self, project_id):
        reports = self._passing_reports(project_id)
        reports["performance"] = PerformanceReport(project_id=project_id, p95_ms=600, error_rate_pct=0.1)
        defects = classify_defects(project_id, True, True, True, **reports)
        report = build_qa_report(project_id, True, True, True, **reports,
                                  tests_total=10, tests_passed=10, tests_failed=0, defects=defects)
        assert report.verdict == QAVerdict.WARN
        assert report.retry_requested is False

    def test_report_includes_coverage_pct(self, project_id):
        reports = self._passing_reports(project_id)
        report = build_qa_report(project_id, True, True, True, **reports,
                                  tests_total=10, tests_passed=10, tests_failed=0, defects=[])
        assert report.coverage_pct == 90.0

    def test_report_defect_ids_match_input_defects(self, project_id):
        reports = self._passing_reports(project_id)
        defects = classify_defects(project_id, False, True, True, **reports)
        report = build_qa_report(project_id, False, True, True, **reports,
                                  tests_total=10, tests_passed=8, tests_failed=2, defects=defects)
        assert set(report.defect_ids) == {d.defect_id for d in defects}


class TestBuildRetryRequest:
    def test_creates_retry_request_with_reason(self, project_id):
        r = build_retry_request(project_id, "engineering", "coverage too low")
        assert r.target_team == "engineering"
        assert r.reason == "coverage too low"
        assert r.can_retry is True


# ══════════════════════════════════════════════════════════════
# LAYER 1d — Unit: routing predicates
# ══════════════════════════════════════════════════════════════

class TestQARouting:
    def test_route_after_validate_inputs_ok(self):
        assert route_after_validate_inputs({"phase_status": "running"}) == "generate"

    def test_route_after_validate_inputs_failed(self):
        assert route_after_validate_inputs({"phase_status": "failed"}) == "failed"

    def test_route_after_generate_ok(self):
        assert route_after_generate({"phase_status": "running"}) == "execute"

    def test_route_after_generate_dlq(self):
        assert route_after_generate({"phase_status": "running", "any_dead_lettered": True}) == "dlq"

    def test_route_after_generate_failed(self):
        assert route_after_generate({"phase_status": "failed"}) == "failed"

    def test_route_after_execute_ok(self):
        assert route_after_execute({"phase_status": "running"}) == "coverage"

    def test_route_after_execute_failed(self):
        assert route_after_execute({"phase_status": "failed"}) == "failed"

    def test_route_after_coverage_always_aggregate(self):
        assert route_after_coverage({}) == "aggregate"

    def test_route_after_aggregate_pass(self):
        assert route_after_aggregate({"verdict": "pass"}) == "publish"

    def test_route_after_aggregate_warn_still_publishes(self):
        assert route_after_aggregate({"verdict": "warn"}) == "publish"

    def test_route_after_aggregate_fail(self):
        assert route_after_aggregate({"verdict": "fail"}) == "defect_report"

    def test_route_after_defect_report_within_budget(self):
        assert route_after_defect_report({"retry_cycles_run": 1}) == "return_to_engineering"

    def test_route_after_defect_report_exhausted(self):
        assert route_after_defect_report({"retry_cycles_run": MAX_RETRY_CYCLES}) == "failed"

    def test_route_task_retry_done(self):
        assert route_task_retry({"status": "completed"}) == "done"

    def test_route_task_retry_retry(self):
        assert route_task_retry({"status": "failed", "retry_count": 1}) == "retry"

    def test_route_task_retry_dead_letter(self):
        assert route_task_retry({"status": "failed", "retry_count": MAX_TASK_RETRIES}) == "dead_letter"

    def test_route_checkpoint_recovery_valid_stage(self):
        assert route_checkpoint_recovery({"resume_at_stage": "execute"}) == "execute"

    def test_route_checkpoint_recovery_invalid_stage_defaults(self):
        assert route_checkpoint_recovery({"resume_at_stage": "bogus"}) == "validate"

    def test_route_checkpoint_recovery_missing_stage_defaults(self):
        assert route_checkpoint_recovery({}) == "validate"


# ══════════════════════════════════════════════════════════════
# LAYER 1e — Unit: agent registry
# ══════════════════════════════════════════════════════════════

class TestQARegistry:
    QA_AGENT_IDS = [
        "qa_head", "unit_test_lead", "unit_test_writer_worker", "coverage_analyzer_worker",
        "integration_test_lead", "integration_test_writer_worker",
        "regression_test_lead", "regression_suite_worker",
        "performance_test_lead", "performance_test_worker",
    ]

    def test_all_qa_agent_ids_registered(self):
        for agent_id in self.QA_AGENT_IDS:
            assert agent_id in AGENT_REGISTRY, f"{agent_id} missing from AGENT_REGISTRY"

    def test_qa_department_has_exactly_ten_agents(self):
        qa_agents = [a for a in AGENT_REGISTRY.values() if a.department == "qa"]
        assert len(qa_agents) == 10

    def test_qa_head_layer_and_parent(self):
        spec = AGENT_REGISTRY["qa_head"]
        assert spec.layer == 3 and spec.role == "head" and spec.parent_agent_id == "manager_agent"

    @pytest.mark.parametrize("lead_id", ["unit_test_lead", "integration_test_lead",
                                          "regression_test_lead", "performance_test_lead"])
    def test_leads_report_to_qa_head(self, lead_id):
        spec = AGENT_REGISTRY[lead_id]
        assert spec.layer == 4 and spec.role == "lead" and spec.parent_agent_id == "qa_head"

    def test_unit_test_writer_reports_to_unit_test_lead(self):
        spec = AGENT_REGISTRY["unit_test_writer_worker"]
        assert spec.parent_agent_id == "unit_test_lead" and spec.layer == 5

    def test_coverage_analyzer_reports_to_unit_test_lead(self):
        spec = AGENT_REGISTRY["coverage_analyzer_worker"]
        assert spec.parent_agent_id == "unit_test_lead"

    def test_integration_writer_reports_to_integration_lead(self):
        spec = AGENT_REGISTRY["integration_test_writer_worker"]
        assert spec.parent_agent_id == "integration_test_lead"

    def test_regression_worker_reports_to_regression_lead(self):
        spec = AGENT_REGISTRY["regression_suite_worker"]
        assert spec.parent_agent_id == "regression_test_lead"

    def test_performance_worker_reports_to_performance_lead(self):
        spec = AGENT_REGISTRY["performance_test_worker"]
        assert spec.parent_agent_id == "performance_test_lead"

    def test_factory_creates_qa_head_with_correct_class(self):
        from core.runtime.factory import AgentFactory
        import services.qa  # noqa: F401 — ensure registration side effects ran
        factory = AgentFactory(db_factory=lambda: None, nats=None, storage=None,
                                audit_repo=None, artifact_repo=None, token_repo=None)
        agent = factory.create("qa_head")
        assert agent.agent_id == "qa_head" and agent.department == "qa"


# ══════════════════════════════════════════════════════════════
# LAYER 2 — Graph: node functions + graph construction
# ══════════════════════════════════════════════════════════════

class TestQAGraph:
    def _base_state(self, project_id="p") -> Dict[str, Any]:
        return {
            "project_id": project_id, "workflow_id": "wf-1", "feature_name": "f",
            "inputs_valid": True, "unit_ready": False, "integration_ready": False,
            "regression_ready": False, "performance_ready": False,
            "coverage_pct": 0.0, "verdict": "pass", "retry_cycles_run": 0,
            "any_dead_lettered": False, "dlq_tasks": [],
            "phase_status": "running", "failure_reason": None, "resume_at_stage": None,
            "nats_events_queue": [], "ws_events_queue": [],
        }

    def test_graph_builds_without_error(self):
        from services.qa.workflows.qa_graph import build_qa_graph
        assert build_qa_graph() is not None

    def test_graph_builds_with_checkpointer_kwarg_path(self):
        from services.qa.workflows.qa_graph import build_qa_graph
        assert build_qa_graph(checkpointer=None) is not None

    @pytest.mark.asyncio
    async def test_receive_artifacts_node_starts_phase(self):
        from services.qa.workflows.qa_graph import receive_artifacts_node
        r = await receive_artifacts_node(self._base_state())
        assert r["phase_status"] == "running"

    @pytest.mark.asyncio
    async def test_validate_inputs_node_valid(self):
        from services.qa.workflows.qa_graph import validate_inputs_node
        r = await validate_inputs_node(self._base_state())
        assert r["phase_status"] == "running"

    @pytest.mark.asyncio
    async def test_validate_inputs_node_invalid(self):
        from services.qa.workflows.qa_graph import validate_inputs_node
        s = self._base_state(); s["inputs_valid"] = False
        r = await validate_inputs_node(s)
        assert r["phase_status"] == "failed" and r["failure_reason"]

    @pytest.mark.asyncio
    async def test_generate_unit_node(self):
        from services.qa.workflows.qa_graph import generate_unit_node
        r = await generate_unit_node(self._base_state())
        assert r["unit_ready"] is True

    @pytest.mark.asyncio
    async def test_generate_integration_node(self):
        from services.qa.workflows.qa_graph import generate_integration_node
        r = await generate_integration_node(self._base_state())
        assert r["integration_ready"] is True

    @pytest.mark.asyncio
    async def test_execute_regression_node(self):
        from services.qa.workflows.qa_graph import execute_regression_node
        r = await execute_regression_node(self._base_state())
        assert r["regression_ready"] is True

    @pytest.mark.asyncio
    async def test_execute_performance_node(self):
        from services.qa.workflows.qa_graph import execute_performance_node
        r = await execute_performance_node(self._base_state())
        assert r["performance_ready"] is True

    @pytest.mark.asyncio
    async def test_coverage_analysis_node_publishes_event(self):
        from services.qa.workflows.qa_graph import coverage_analysis_node
        r = await coverage_analysis_node(self._base_state())
        subjects = [e["subject"] for e in r["nats_events_queue"]]
        assert "qa.coverage.completed" in subjects

    @pytest.mark.asyncio
    async def test_aggregate_results_all_ready(self):
        from services.qa.workflows.qa_graph import aggregate_results_node
        s = self._base_state()
        s.update(unit_ready=True, integration_ready=True, regression_ready=True, performance_ready=True)
        r = await aggregate_results_node(s)
        assert r["phase_status"] == "running"

    @pytest.mark.asyncio
    async def test_aggregate_results_missing_team_fails(self):
        from services.qa.workflows.qa_graph import aggregate_results_node
        s = self._base_state()
        s.update(unit_ready=True, integration_ready=False, regression_ready=True, performance_ready=True)
        r = await aggregate_results_node(s)
        assert r["phase_status"] == "failed"

    @pytest.mark.asyncio
    async def test_defect_report_node_increments_cycle(self):
        from services.qa.workflows.qa_graph import defect_report_node
        s = self._base_state(); s["retry_cycles_run"] = 1
        r = await defect_report_node(s)
        assert r["retry_cycles_run"] == 2

    @pytest.mark.asyncio
    async def test_return_to_engineering_node_publishes_retry_event(self):
        from services.qa.workflows.qa_graph import return_to_engineering_node
        r = await return_to_engineering_node(self._base_state())
        subjects = [e["subject"] for e in r["nats_events_queue"]]
        assert "qa.retry.requested" in subjects

    @pytest.mark.asyncio
    async def test_dlq_node_sets_failed_with_reason(self):
        from services.qa.workflows.qa_graph import dlq_node
        s = self._base_state(); s["dlq_tasks"] = ["t1", "t2"]
        r = await dlq_node(s)
        assert r["phase_status"] == "failed" and "t1" in r["failure_reason"]

    @pytest.mark.asyncio
    async def test_publish_artifacts_node_completes_phase(self):
        from services.qa.workflows.qa_graph import publish_artifacts_node
        r = await publish_artifacts_node(self._base_state())
        subjects = [e["subject"] for e in r["nats_events_queue"]]
        assert r["phase_status"] == "completed"
        assert "qa.phase.completed" in subjects

    @pytest.mark.asyncio
    async def test_handle_failure_node_publishes_failure_event(self):
        from services.qa.workflows.qa_graph import handle_failure_node
        s = self._base_state(); s["failure_reason"] = "boom"
        r = await handle_failure_node(s)
        subjects = [e["subject"] for e in r["nats_events_queue"]]
        assert "qa.phase.failed" in subjects


# ══════════════════════════════════════════════════════════════
# LAYER 3a — Integration: read-only Repository Service client
# ══════════════════════════════════════════════════════════════

class TestQARepositoryReadClient:
    def _mock_client(self, json_body: Any, status: int = 200):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = json_body
        resp.text = json.dumps(json_body)

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        return mock_client

    @pytest.mark.asyncio
    async def test_get_repository_calls_correct_path(self):
        client = QARepositoryReadClient(base_url="http://test")
        mock_client = self._mock_client({"id": "repo-1"})
        with patch("services.qa.integration.repository_client.httpx.AsyncClient", return_value=mock_client):
            result = await client.get_repository("p1")
        mock_client.get.assert_called_once_with("/repositories/p1")
        assert result["id"] == "repo-1"

    @pytest.mark.asyncio
    async def test_list_branches_calls_correct_path(self):
        client = QARepositoryReadClient(base_url="http://test")
        mock_client = self._mock_client([{"name": "integration/f"}])
        with patch("services.qa.integration.repository_client.httpx.AsyncClient", return_value=mock_client):
            result = await client.list_branches("p1")
        mock_client.get.assert_called_once_with("/branches/p1")
        assert result[0]["name"] == "integration/f"

    @pytest.mark.asyncio
    async def test_list_pull_requests_calls_correct_path(self):
        client = QARepositoryReadClient(base_url="http://test")
        mock_client = self._mock_client([{"id": "pr-1"}])
        with patch("services.qa.integration.repository_client.httpx.AsyncClient", return_value=mock_client):
            result = await client.list_pull_requests("p1")
        mock_client.get.assert_called_once_with("/pull-requests/p1")
        assert result[0]["id"] == "pr-1"

    @pytest.mark.asyncio
    async def test_get_release_history_calls_correct_path(self):
        client = QARepositoryReadClient(base_url="http://test")
        mock_client = self._mock_client([{"event_type": "release.created"}])
        with patch("services.qa.integration.repository_client.httpx.AsyncClient", return_value=mock_client):
            result = await client.get_release_history("p1")
        mock_client.get.assert_called_once_with("/releases/p1/history")
        assert result[0]["event_type"] == "release.created"

    @pytest.mark.asyncio
    async def test_get_commit_history_filters_commit_events(self):
        client = QARepositoryReadClient(base_url="http://test")
        mock_client = self._mock_client([
            {"event_type": "commit.created"}, {"event_type": "release.created"},
        ])
        with patch("services.qa.integration.repository_client.httpx.AsyncClient", return_value=mock_client):
            result = await client.get_commit_history("repo-1")
        assert len(result) == 1 and result[0]["event_type"] == "commit.created"

    @pytest.mark.asyncio
    async def test_error_response_raises_client_error_with_status(self):
        client = QARepositoryReadClient(base_url="http://test")
        mock_client = self._mock_client({"detail": "not found"}, status=404)
        with patch("services.qa.integration.repository_client.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(RepositoryServiceClientError) as exc_info:
                await client.get_repository("missing")
        assert exc_info.value.status_code == 404

    def test_default_base_url_uses_settings_port(self):
        client = QARepositoryReadClient()
        assert "8006" in client._base_url

    def test_client_has_no_write_methods(self):
        write_verbs = ("create", "commit_files", "merge", "approve", "delete", "push")
        public_methods = [m for m in dir(QARepositoryReadClient) if not m.startswith("_")]
        for m in public_methods:
            assert not any(v in m for v in write_verbs), f"QA client exposes write-capable method: {m}"


# ══════════════════════════════════════════════════════════════
# LAYER 3b — Integration: QA workers (mocked LLM)
# ══════════════════════════════════════════════════════════════

MOCK_UNIT_TESTS = json.dumps({
    "files": [{"path": "tests/unit/test_auth.py", "language": "python",
               "content": "def test_login():\n    assert True"}],
    "test_count": 12, "functions_covered": ["login"], "quality_score": 0.88,
})
MOCK_INTEGRATION_TESTS = json.dumps({
    "files": [{"path": "tests/integration/test_api.py", "language": "python",
               "content": "def test_health():\n    assert True"}],
    "test_count": 3, "endpoints_covered": ["/health"], "quality_score": 0.87,
})
MOCK_CRITIQUE_PASS = json.dumps({"passed": True, "score": 0.9, "blocking": [], "warnings": [], "suggestions": []})
MOCK_CRITIQUE_FAIL = json.dumps({"passed": False, "score": 0.4, "blocking": ["missing edge case"],
                                  "warnings": [], "suggestions": ["add edge case test"]})


class TestUnitTeamWorkers:
    @pytest.mark.asyncio
    async def test_unit_test_writer_generates_suite(self, qa_task):
        from services.qa.workers.unit import UnitTestWriterWorker
        infra = make_infra()
        agent = inject(UnitTestWriterWorker, infra, "unit_test_writer_worker")
        p1, p2, p3 = patched(agent, MOCK_UNIT_TESTS)
        with p1, p2, p3:
            result = await agent.execute(qa_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["suite_type"] == "unit"
        assert len(result.artifacts) == 1

    @pytest.mark.asyncio
    async def test_unit_test_writer_includes_idempotent_key(self, qa_task):
        from services.qa.workers.unit import UnitTestWriterWorker
        infra = make_infra()
        agent = inject(UnitTestWriterWorker, infra, "unit_test_writer_worker")
        p1, p2, p3 = patched(agent, MOCK_UNIT_TESTS)
        with p1, p2, p3:
            result = await agent.execute(qa_task)
        assert result.content.get("suite_id")

    @pytest.mark.asyncio
    async def test_unit_test_writer_escalates_on_persistent_review_failure(self, qa_task):
        from services.qa.workers.unit import UnitTestWriterWorker
        infra = make_infra()
        agent = inject(UnitTestWriterWorker, infra, "unit_test_writer_worker")
        with patch.object(agent, "_pre_execute", AsyncMock()), \
             patch.object(agent, "_post_execute", AsyncMock()), \
             patch.object(agent, "call_llm", AsyncMock(side_effect=[
                 ('{"files": [], "test_count": 0, "quality_score": 0.0}', None),
                 (MOCK_CRITIQUE_FAIL, None), ('{"files": [], "test_count": 0}', None),
                 (MOCK_CRITIQUE_FAIL, None), ('{"files": [], "test_count": 0}', None),
                 (MOCK_CRITIQUE_FAIL, None), ('{"files": [], "test_count": 0}', None),
             ])):
            result = await agent.execute(qa_task)
        assert result.status == TaskStatus.ESCALATED

    @pytest.mark.asyncio
    async def test_coverage_analyzer_passes_with_sufficient_tests(self, qa_task):
        from services.qa.workers.unit import CoverageAnalyzerWorker
        infra = make_infra()
        agent = inject(CoverageAnalyzerWorker, infra, "coverage_analyzer_worker")
        qa_task.context.approved_artifacts["unit_test_suite"] = {"test_count": 30}
        result = await agent.execute(qa_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["meets_threshold"] is True

    @pytest.mark.asyncio
    async def test_coverage_analyzer_fails_with_insufficient_tests(self, qa_task):
        from services.qa.workers.unit import CoverageAnalyzerWorker
        infra = make_infra()
        agent = inject(CoverageAnalyzerWorker, infra, "coverage_analyzer_worker")
        qa_task.context.approved_artifacts["unit_test_suite"] = {"test_count": 0}
        result = await agent.execute(qa_task)
        assert result.status == TaskStatus.FAILED
        assert result.content["meets_threshold"] is False
        assert result.failure_reason is not None

    @pytest.mark.asyncio
    async def test_coverage_analyzer_respects_custom_threshold(self, qa_task):
        from services.qa.workers.unit import CoverageAnalyzerWorker
        infra = make_infra()
        agent = inject(CoverageAnalyzerWorker, infra, "coverage_analyzer_worker")
        qa_task.context.approved_artifacts["unit_test_suite"] = {"test_count": 5}
        qa_task.context.approved_artifacts["__coverage_threshold__"] = 50.0
        result = await agent.execute(qa_task)
        assert result.content["threshold_pct"] == 50.0


class TestIntegrationTeamWorker:
    @pytest.mark.asyncio
    async def test_integration_writer_generates_suite(self, qa_task):
        from services.qa.workers.integration import IntegrationTestWriterWorker
        infra = make_infra()
        agent = inject(IntegrationTestWriterWorker, infra, "integration_test_writer_worker")
        p1, p2, p3 = patched(agent, MOCK_INTEGRATION_TESTS)
        with p1, p2, p3:
            result = await agent.execute(qa_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["suite_type"] == "integration"

    @pytest.mark.asyncio
    async def test_integration_writer_contract_valid_with_paths(self, qa_task):
        from services.qa.workers.integration import IntegrationTestWriterWorker
        infra = make_infra()
        agent = inject(IntegrationTestWriterWorker, infra, "integration_test_writer_worker")
        p1, p2, p3 = patched(agent, MOCK_INTEGRATION_TESTS)
        with p1, p2, p3:
            result = await agent.execute(qa_task)
        assert result.content["contract_valid"] is True

    @pytest.mark.asyncio
    async def test_integration_writer_contract_invalid_without_paths(self, qa_task):
        from services.qa.workers.integration import IntegrationTestWriterWorker
        infra = make_infra()
        agent = inject(IntegrationTestWriterWorker, infra, "integration_test_writer_worker")
        qa_task.context.approved_artifacts["openapi_spec"] = {"paths": {}}
        p1, p2, p3 = patched(agent, MOCK_INTEGRATION_TESTS)
        with p1, p2, p3:
            result = await agent.execute(qa_task)
        assert result.content["contract_valid"] is False


class TestRegressionWorker:
    @pytest.mark.asyncio
    async def test_regression_worker_passes_with_no_known_regressions(self, qa_task):
        from services.qa.workers.regression import RegressionSuiteWorker
        infra = make_infra()
        agent = inject(RegressionSuiteWorker, infra, "regression_suite_worker")
        qa_task.context.approved_artifacts["unit_test_suite"] = {"test_count": 10}
        qa_task.context.approved_artifacts["integration_test_suite"] = {"test_count": 5}
        result = await agent.execute(qa_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["tests_run"] == 15

    @pytest.mark.asyncio
    async def test_regression_worker_fails_with_known_regressions(self, qa_task):
        from services.qa.workers.regression import RegressionSuiteWorker
        infra = make_infra()
        agent = inject(RegressionSuiteWorker, infra, "regression_suite_worker")
        qa_task.context.approved_artifacts["__known_regressions__"] = ["test_login_regression"]
        result = await agent.execute(qa_task)
        assert result.status == TaskStatus.FAILED
        assert "test_login_regression" in result.failure_reason


class TestPerformanceWorker:
    @pytest.mark.asyncio
    async def test_performance_worker_passes_under_threshold(self, qa_task):
        from services.qa.workers.performance import PerformanceTestWorker
        infra = make_infra()
        agent = inject(PerformanceTestWorker, infra, "performance_test_worker")
        result = await agent.execute(qa_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["p95_ms"] < 500

    @pytest.mark.asyncio
    async def test_performance_worker_fails_with_override_above_threshold(self, qa_task):
        from services.qa.workers.performance import PerformanceTestWorker
        infra = make_infra()
        agent = inject(PerformanceTestWorker, infra, "performance_test_worker")
        qa_task.context.approved_artifacts["__perf_p95_override_ms__"] = 900
        result = await agent.execute(qa_task)
        assert result.status == TaskStatus.FAILED
        assert result.failure_reason is not None


# ══════════════════════════════════════════════════════════════
# LAYER 3c — Integration: QA leads (fake factory)
# ══════════════════════════════════════════════════════════════

class TestUnitTestLead:
    @pytest.mark.asyncio
    async def test_success(self, qa_task):
        from services.qa.leads import UnitTestLead
        infra = make_infra()
        lead = inject(UnitTestLead, infra, "unit_test_lead", layer=4, role="lead")
        factory = FakeFactory({w: ok_result(w) for w in UNIT_WORKERS})
        qa_task.context.approved_artifacts["__factory__"] = factory
        result = await lead.execute(qa_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["suites"] == len(UNIT_WORKERS)

    @pytest.mark.asyncio
    async def test_reports_worker_failures_without_hard_escalation(self, qa_task):
        from services.qa.leads import UnitTestLead
        infra = make_infra()
        lead = inject(UnitTestLead, infra, "unit_test_lead", layer=4, role="lead")
        results = {w: ok_result(w) for w in UNIT_WORKERS}
        results["coverage_analyzer_worker"] = fail_result("coverage_analyzer_worker", "below threshold")
        factory = FakeFactory(results)
        qa_task.context.approved_artifacts["__factory__"] = factory
        result = await lead.execute(qa_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["failures"]


class TestIntegrationTestLead:
    @pytest.mark.asyncio
    async def test_success(self, qa_task):
        from services.qa.leads import IntegrationTestLead
        infra = make_infra()
        lead = inject(IntegrationTestLead, infra, "integration_test_lead", layer=4, role="lead")
        factory = FakeFactory({w: ok_result(w) for w in INTEGRATION_WORKERS})
        qa_task.context.approved_artifacts["__factory__"] = factory
        result = await lead.execute(qa_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["suites"] == len(INTEGRATION_WORKERS)


class TestRegressionTestLead:
    @pytest.mark.asyncio
    async def test_success(self, qa_task):
        from services.qa.leads import RegressionTestLead
        infra = make_infra()
        lead = inject(RegressionTestLead, infra, "regression_test_lead", layer=4, role="lead")
        factory = FakeFactory({w: ok_result(w) for w in REGRESSION_WORKERS})
        qa_task.context.approved_artifacts["__factory__"] = factory
        result = await lead.execute(qa_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["suites"] == len(REGRESSION_WORKERS)


class TestPerformanceTestLead:
    @pytest.mark.asyncio
    async def test_success(self, qa_task):
        from services.qa.leads import PerformanceTestLead
        infra = make_infra()
        lead = inject(PerformanceTestLead, infra, "performance_test_lead", layer=4, role="lead")
        factory = FakeFactory({w: ok_result(w) for w in PERFORMANCE_WORKERS})
        qa_task.context.approved_artifacts["__factory__"] = factory
        result = await lead.execute(qa_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["suites"] == len(PERFORMANCE_WORKERS)


# ══════════════════════════════════════════════════════════════
# LAYER 4 — E2E: QAHead full pipeline (fake factory chain)
# ══════════════════════════════════════════════════════════════

@pytest.mark.e2e
class TestQAHeadPipeline:
    def _full_success_factory(self) -> FakeFactory:
        return FakeFactory({
            "unit_test_lead": ok_result("unit_test_lead", team="unit", suites=2),
            "integration_test_lead": ok_result("integration_test_lead", team="integration", suites=1),
            "regression_test_lead": ok_result("regression_test_lead", team="regression", suites=1),
            "performance_test_lead": ok_result("performance_test_lead", team="performance", suites=1),
        })

    def _populate_passing_artifacts(self, task, project_id):
        task.context.approved_artifacts["unit_test_suite"] = {"test_count": 20}
        task.context.approved_artifacts["integration_test_suite"] = {"test_count": 5}
        task.context.approved_artifacts["integration_test_writer_worker"] = {"contract_valid": True}
        task.context.approved_artifacts["coverage_report"] = CoverageReport.build(project_id, 90.0).model_dump()
        task.context.approved_artifacts["regression_report"] = RegressionReport(
            project_id=project_id, tests_run=25, tests_passed=25, tests_failed=0).model_dump()
        task.context.approved_artifacts["performance_report"] = PerformanceReport(
            project_id=project_id, p95_ms=300, error_rate_pct=0.1).model_dump()

    @pytest.mark.asyncio
    async def test_full_pipeline_passes(self, qa_task, project_id):
        from services.qa.head import QAHead
        infra = make_infra()
        head = inject(QAHead, infra, "qa_head", layer=3, role="head")
        qa_task.context.approved_artifacts["__factory__"] = self._full_success_factory()
        self._populate_passing_artifacts(qa_task, project_id)
        result = await head.execute(qa_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["verdict"] == "pass"
        assert any(e.subject == "qa.phase.completed" for e in result.nats_events)

    @pytest.mark.asyncio
    async def test_pipeline_escalates_when_required_input_missing(self, qa_task_missing_inputs):
        from services.qa.head import QAHead
        infra = make_infra()
        head = inject(QAHead, infra, "qa_head", layer=3, role="head")
        qa_task_missing_inputs.context.approved_artifacts["__factory__"] = self._full_success_factory()
        result = await head.execute(qa_task_missing_inputs)
        assert result.status == TaskStatus.ESCALATED

    @pytest.mark.asyncio
    async def test_pipeline_fails_with_coverage_gap(self, qa_task, project_id):
        from services.qa.head import QAHead
        infra = make_infra()
        head = inject(QAHead, infra, "qa_head", layer=3, role="head")
        qa_task.context.approved_artifacts["__factory__"] = self._full_success_factory()
        self._populate_passing_artifacts(qa_task, project_id)
        qa_task.context.approved_artifacts["coverage_report"] = CoverageReport.build(project_id, 40.0).model_dump()
        result = await head.execute(qa_task)
        assert result.status == TaskStatus.FAILED
        assert result.content["verdict"] == "fail"
        assert result.content["retry_request"] is not None

    @pytest.mark.asyncio
    async def test_pipeline_fails_and_publishes_defect_events(self, qa_task, project_id):
        from services.qa.head import QAHead
        infra = make_infra()
        head = inject(QAHead, infra, "qa_head", layer=3, role="head")
        qa_task.context.approved_artifacts["__factory__"] = self._full_success_factory()
        self._populate_passing_artifacts(qa_task, project_id)
        qa_task.context.approved_artifacts["regression_report"] = RegressionReport(
            project_id=project_id, tests_run=25, tests_passed=23, tests_failed=2,
            regressions_detected=["test_checkout"]).model_dump()
        result = await head.execute(qa_task)
        assert result.status == TaskStatus.FAILED
        assert len(result.content["defects"]) >= 1
        assert any(e.subject == "qa.phase.failed" for e in result.nats_events)

    @pytest.mark.asyncio
    async def test_pipeline_publishes_websocket_completion_event(self, qa_task, project_id):
        from services.qa.head import QAHead
        infra = make_infra()
        head = inject(QAHead, infra, "qa_head", layer=3, role="head")
        qa_task.context.approved_artifacts["__factory__"] = self._full_success_factory()
        self._populate_passing_artifacts(qa_task, project_id)
        result = await head.execute(qa_task)
        assert any(e.event_type == "phase_completed" for e in result.ws_events)

    @pytest.mark.asyncio
    async def test_pipeline_stores_qa_plan_in_context(self, qa_task, project_id):
        from services.qa.head import QAHead
        infra = make_infra()
        head = inject(QAHead, infra, "qa_head", layer=3, role="head")
        qa_task.context.approved_artifacts["__factory__"] = self._full_success_factory()
        self._populate_passing_artifacts(qa_task, project_id)
        await head.execute(qa_task)
        assert "__qa_plan__" in qa_task.context.approved_artifacts

    @pytest.mark.asyncio
    async def test_pipeline_runs_without_factory_using_placeholders(self, qa_task, project_id):
        from services.qa.head import QAHead
        infra = make_infra()
        head = inject(QAHead, infra, "qa_head", layer=3, role="head")
        self._populate_passing_artifacts(qa_task, project_id)
        result = await head.execute(qa_task)
        assert result.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)

    @pytest.mark.asyncio
    async def test_pipeline_creates_qa_report_artifact(self, qa_task, project_id):
        from services.qa.head import QAHead
        infra = make_infra()
        head = inject(QAHead, infra, "qa_head", layer=3, role="head")
        qa_task.context.approved_artifacts["__factory__"] = self._full_success_factory()
        self._populate_passing_artifacts(qa_task, project_id)
        result = await head.execute(qa_task)
        artifact_types = [a.artifact_type for a in result.artifacts]
        assert "qa_report" in artifact_types

    @pytest.mark.asyncio
    async def test_pipeline_warn_verdict_still_completes(self, qa_task, project_id):
        from services.qa.head import QAHead
        infra = make_infra()
        head = inject(QAHead, infra, "qa_head", layer=3, role="head")
        qa_task.context.approved_artifacts["__factory__"] = self._full_success_factory()
        self._populate_passing_artifacts(qa_task, project_id)
        qa_task.context.approved_artifacts["performance_report"] = PerformanceReport(
            project_id=project_id, p95_ms=600, error_rate_pct=0.1).model_dump()
        result = await head.execute(qa_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["verdict"] == "warn"
