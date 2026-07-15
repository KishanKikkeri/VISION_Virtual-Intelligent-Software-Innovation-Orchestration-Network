"""
tests/foundation/test_m33_engineering.py
===========================================
M3.3 Engineering Service tests — 4 layers matching the M3.1/M3.2 pattern.

Layer 1 — Unit:        models, utils, task decomposition, routing predicates,
                       agent registry + Appendix A patch verification
Layer 2 — Graph:       LangGraph node functions + graph construction
Layer 3 — Integration: workers (mocked LLM), Repository Service client
                       (mocked httpx), leads (fake factory)
Layer 4 — E2E:         full EngineeringHead pipeline (fake factory chain)
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.contracts import AgentResult, TaskStatus
from core.runtime.context import AgentContext, TaskInput
from core.runtime.factory import AGENT_REGISTRY

from services.engineering.models import (
    BuildResult,
    CodeFile,
    CodeModule,
    EngineeringTask,
    EngineeringTeam,
    EngineeringTaskStatus,
    ImplementationPlan,
    ModuleType,
    ReviewResult,
    ReviewVerdict,
)
from services.engineering.utils import (
    exponential_backoff_seconds,
    files_to_dicts,
    idempotency_key,
    parse_llm_json,
    quality_gate,
    summarize_failures,
)
from services.engineering.context import (
    BACKEND_WORKERS,
    FRONTEND_WORKERS,
    INTEGRATION_WORKERS,
    build_implementation_plan,
    team_progress,
    topological_batches,
)
from services.engineering.routing import (
    MAX_REVIEW_CYCLES,
    MAX_TASK_RETRIES,
    route_after_aggregate,
    route_after_fan_out,
    route_after_plan,
    route_after_repository,
    route_after_review,
    route_checkpoint_recovery,
    route_task_retry,
)
from services.engineering.integration.repository_client import (
    RepositoryServiceClient,
    RepositoryServiceClientError,
)
from services.engineering.schemas import EngineeringServiceError

from services.repository.schemas import BranchType, InvalidBranchNameError
from services.repository.managers import build_branch_name


# ══════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════

@pytest.fixture
def project_id() -> str:
    return str(uuid.uuid4())


UI_BLUEPRINT = {
    "pages": [{"name": "Dashboard", "route": "/dashboard"}],
    "routes": [{"path": "/dashboard", "page": "Dashboard", "auth_required": True}],
    "components": [{"name": "NavBar", "type": "layout"}],
    "layouts": [{"name": "AppShell"}],
    "navigation": {"primary": [{"label": "Dashboard", "route": "/dashboard"}]},
    "forms": [], "tables": [], "user_flows": [],
    "state_boundaries": [{"scope": "global", "owner": "AuthStore"}],
    "api_bindings": [],
}


@pytest.fixture
def eng_context(project_id) -> AgentContext:
    return AgentContext(
        project_id=project_id, workflow_id=str(uuid.uuid4()),
        current_phase=4, project_name="TestApp",
        project_description="A test SaaS app.",
        approved_artifacts={
            "architecture_blueprint": {"services": [{"name": "api"}, {"name": "web"}]},
            "openapi_spec": {"paths": {"/health": {}, "/auth/login": {}}},
            "database_schema": {"tables": [{"name": "users"}, {"name": "projects"}]},
            "ui_blueprint": UI_BLUEPRINT,
            "user_stories_doc": {"user_stories": [{"id": "US-001", "action": "login"}]},
            "integration_plan": {"providers": [{"name": "sendgrid"}]},
        },
        tech_stack={"backend": "Python+FastAPI", "frontend": "React+TS"},
        llm_provider="anthropic", llm_model="claude-sonnet-4-6",
        budget_limit_usd=50.0, total_spend_usd=2.0,
    )


@pytest.fixture
def eng_context_no_ui(eng_context) -> AgentContext:
    eng_context.approved_artifacts = {
        k: v for k, v in eng_context.approved_artifacts.items() if k != "ui_blueprint"
    }
    return eng_context


@pytest.fixture
def eng_task(project_id, eng_context) -> TaskInput:
    return TaskInput(
        task_id=str(uuid.uuid4()), project_id=project_id,
        agent_id="engineering_head", parent_agent_id="manager_agent",
        task_type="run_engineering_pipeline",
        description="Implement approved architecture",
        expected_output="Merge-ready pull request",
        context=eng_context,
    )


@pytest.fixture
def eng_task_no_ui(project_id, eng_context_no_ui) -> TaskInput:
    return TaskInput(
        task_id=str(uuid.uuid4()), project_id=project_id,
        agent_id="engineering_head", parent_agent_id="manager_agent",
        task_type="run_engineering_pipeline",
        description="Implement approved architecture (no UI)",
        expected_output="Merge-ready pull request",
        context=eng_context_no_ui,
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


def inject(agent_class, infra, agent_id: str):
    a = agent_class.__new__(agent_class)
    a.agent_id = agent_id
    a.name = agent_class.__name__
    a.department = "engineering"
    a.layer = 5
    a.role = "worker"
    a.responsibilities = ["Engineering implementation"]
    a._db_factory = infra["db_factory"]; a._nats = infra["nats"]; a._storage = infra["storage"]
    a._audit_repo = infra["audit_repo"]; a._artifact_repo = infra["artifact_repo"]
    a._token_repo = infra["token_repo"]; a._qdrant = None
    return a


async def _noop(*args, **kwargs):
    return None


def patched(agent, raw_response, usage=None):
    """Context-manager-free helper: patches call_llm/_pre_execute/_post_execute."""
    return (
        patch.object(agent, "call_llm", AsyncMock(return_value=(raw_response, usage))),
        patch.object(agent, "_pre_execute", AsyncMock()),
        patch.object(agent, "_post_execute", AsyncMock()),
    )


class FakeAgent:
    """Stand-in returned by FakeFactory.create() — always returns a fixed AgentResult."""
    def __init__(self, agent_id: str, result: AgentResult):
        self.agent_id = agent_id
        self._result = result

    async def run(self, task: TaskInput) -> AgentResult:
        return self._result


class FakeFactory:
    """Maps agent_id -> AgentResult (or a callable(task) -> AgentResult)."""
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
                        content=content or {"module_id": agent_id, "files": [{"path": "x.py", "content": "x"}]},
                        quality_score=0.9,
                        artifacts=[{"artifact_id": "a1", "artifact_type": "source_code", "version": 1}])


def fail_result(agent_id: str, reason: str) -> AgentResult:
    return AgentResult(task_id="t", agent_id=agent_id, status=TaskStatus.FAILED,
                        content={}, quality_score=0.0, failure_reason=reason)


# ══════════════════════════════════════════════════════════════
# LAYER 1a — Unit: models
# ══════════════════════════════════════════════════════════════

class TestCodeModule:
    def test_is_buildable_true_with_files_and_score(self):
        m = CodeModule(project_id="p", task_id="t", module_type=ModuleType.API_ENDPOINT,
                        files=[CodeFile(path="a.py", language="python", content="x")],
                        quality_score=0.8, generated_by="w")
        assert m.is_buildable is True

    def test_is_buildable_false_without_files(self):
        m = CodeModule(project_id="p", task_id="t", module_type=ModuleType.API_ENDPOINT,
                        files=[], quality_score=0.9, generated_by="w")
        assert m.is_buildable is False

    def test_is_buildable_false_with_zero_score(self):
        m = CodeModule(project_id="p", task_id="t", module_type=ModuleType.API_ENDPOINT,
                        files=[CodeFile(path="a.py", language="python", content="x")],
                        quality_score=0.0, generated_by="w")
        assert m.is_buildable is False

    def test_coding_contract_no_violations_when_complete(self):
        m = CodeModule(project_id="p", task_id="t", module_type=ModuleType.DATABASE,
                        files=[CodeFile(path="a.py", language="python", content="x")],
                        quality_score=0.9, generated_by="w", idempotent_key="abc123")
        assert m.satisfies_coding_contract() == []

    def test_coding_contract_flags_missing_files(self):
        m = CodeModule(project_id="p", task_id="t", module_type=ModuleType.DATABASE,
                        files=[], quality_score=0.9, generated_by="w", idempotent_key="abc")
        assert "buildable" in m.satisfies_coding_contract()

    def test_coding_contract_flags_low_score(self):
        m = CodeModule(project_id="p", task_id="t", module_type=ModuleType.DATABASE,
                        files=[CodeFile(path="a.py", language="python", content="x")],
                        quality_score=0.5, generated_by="w", idempotent_key="abc")
        assert "reviewable" in m.satisfies_coding_contract()

    def test_coding_contract_flags_missing_idempotent_key(self):
        m = CodeModule(project_id="p", task_id="t", module_type=ModuleType.DATABASE,
                        files=[CodeFile(path="a.py", language="python", content="x")],
                        quality_score=0.9, generated_by="w", idempotent_key=None)
        assert "idempotent" in m.satisfies_coding_contract()

    def test_module_type_enum_values_cover_all_worker_domains(self):
        expected = {"database", "auth", "business_logic", "api_endpoint", "component",
                    "page", "state", "routing", "internal_event", "external_api", "messaging"}
        assert {m.value for m in ModuleType} == expected


class TestEngineeringTask:
    def test_can_run_with_no_dependencies(self):
        t = EngineeringTask(project_id="p", team=EngineeringTeam.BACKEND, worker_agent_id="w")
        assert t.can_run(set()) is True

    def test_can_run_false_when_dependency_incomplete(self):
        t = EngineeringTask(project_id="p", team=EngineeringTeam.BACKEND, worker_agent_id="w",
                             depends_on=["dep-1"])
        assert t.can_run(set()) is False

    def test_can_run_true_when_dependency_complete(self):
        t = EngineeringTask(project_id="p", team=EngineeringTeam.BACKEND, worker_agent_id="w",
                             depends_on=["dep-1"])
        assert t.can_run({"dep-1"}) is True

    def test_can_run_false_when_not_pending(self):
        t = EngineeringTask(project_id="p", team=EngineeringTeam.BACKEND, worker_agent_id="w",
                             status=EngineeringTaskStatus.RUNNING)
        assert t.can_run(set()) is False

    @pytest.mark.parametrize("retries,expected", [(0, 1), (1, 2), (2, 4), (3, 8), (10, 60)])
    def test_backoff_seconds_exponential_capped(self, retries, expected):
        t = EngineeringTask(project_id="p", team=EngineeringTeam.BACKEND, worker_agent_id="w",
                             retry_count=retries)
        assert t.next_backoff_seconds() == expected


class TestImplementationPlan:
    def _plan(self):
        t1 = EngineeringTask(project_id="p", team=EngineeringTeam.BACKEND, worker_agent_id="database_layer_worker")
        t2 = EngineeringTask(project_id="p", team=EngineeringTeam.BACKEND, worker_agent_id="authentication_worker",
                              depends_on=[t1.task_id])
        t3 = EngineeringTask(project_id="p", team=EngineeringTeam.FRONTEND, worker_agent_id="component_worker",
                              status=EngineeringTaskStatus.COMPLETED)
        return ImplementationPlan(project_id="p", feature_name="f", tasks=[t1, t2, t3]), t1, t2, t3

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
        backend = plan.tasks_by_team(EngineeringTeam.BACKEND)
        assert t1 in backend and t2 in backend and t3 not in backend

    def test_all_complete_false_when_pending_tasks_remain(self):
        plan, *_ = self._plan()
        assert plan.all_complete is False

    def test_all_complete_true_when_everything_completed(self):
        t = EngineeringTask(project_id="p", team=EngineeringTeam.BACKEND, worker_agent_id="w",
                             status=EngineeringTaskStatus.COMPLETED)
        plan = ImplementationPlan(project_id="p", feature_name="f", tasks=[t])
        assert plan.all_complete is True

    def test_any_dead_lettered_false_by_default(self):
        plan, *_ = self._plan()
        assert plan.any_dead_lettered is False

    def test_any_dead_lettered_true_when_flagged(self):
        t = EngineeringTask(project_id="p", team=EngineeringTeam.BACKEND, worker_agent_id="w",
                             dead_lettered=True)
        plan = ImplementationPlan(project_id="p", feature_name="f", tasks=[t])
        assert plan.any_dead_lettered is True


class TestReviewAndBuildResult:
    def test_review_result_defaults(self):
        r = ReviewResult(module_id="m1", verdict=ReviewVerdict.PASS, score=0.9, reviewed_by="code_reviewer_worker")
        assert r.cycle == 1 and r.blocking_issues == []

    def test_build_result_defaults(self):
        b = BuildResult(project_id="p", feature_name="f", passed=True)
        assert b.modules_checked == 0 and b.merged is False and b.commit_shas == []

    def test_review_verdict_enum_values(self):
        assert {v.value for v in ReviewVerdict} == {"pass", "revise", "block"}


# ══════════════════════════════════════════════════════════════
# LAYER 1b — Unit: utils
# ══════════════════════════════════════════════════════════════

class TestParseLlmJson:
    def test_parses_plain_json(self):
        assert parse_llm_json('{"a": 1}') == {"a": 1}

    def test_parses_fenced_json_with_lang_tag(self):
        assert parse_llm_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_parses_fenced_json_without_lang_tag(self):
        assert parse_llm_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_returns_fallback_on_invalid_json(self):
        assert parse_llm_json("not json", {"x": 1}) == {"x": 1}

    def test_returns_empty_dict_when_no_fallback_given(self):
        assert parse_llm_json("not json") == {}


class TestIdempotencyKey:
    def test_deterministic_for_same_inputs(self):
        a = idempotency_key("p1", "t1", "w1")
        b = idempotency_key("p1", "t1", "w1")
        assert a == b

    def test_differs_for_different_task(self):
        a = idempotency_key("p1", "t1", "w1")
        b = idempotency_key("p1", "t2", "w1")
        assert a != b

    def test_key_length_is_16(self):
        assert len(idempotency_key("p", "t", "w")) == 16


class TestQualityGate:
    def test_passes_at_threshold(self):
        assert quality_gate(0.7, 0.7) is True

    def test_fails_below_threshold(self):
        assert quality_gate(0.69, 0.7) is False

    def test_passes_above_threshold(self):
        assert quality_gate(0.95) is True


class TestExponentialBackoff:
    @pytest.mark.parametrize("retries,expected", [(0, 1), (1, 2), (5, 32), (6, 60), (20, 60)])
    def test_backoff_values(self, retries, expected):
        assert exponential_backoff_seconds(retries) == expected

    def test_negative_retry_count_clamped(self):
        assert exponential_backoff_seconds(-5) == 1


class TestFilesToDicts:
    def test_converts_dict_input(self):
        out = files_to_dicts([{"path": "a.py", "content": "x"}])
        assert out == [{"path": "a.py", "content": "x", "mode": "100644"}]

    def test_converts_codefile_objects(self):
        out = files_to_dicts([CodeFile(path="a.py", language="python", content="x")])
        assert out[0]["path"] == "a.py" and out[0]["mode"] == "100644"

    def test_preserves_custom_mode(self):
        out = files_to_dicts([{"path": "a.sh", "content": "x", "mode": "100755"}])
        assert out[0]["mode"] == "100755"


class TestSummarizeFailures:
    def test_empty_list_returns_placeholder(self):
        assert summarize_failures([]) == "no failure detail available"

    def test_joins_up_to_five_reasons(self):
        items = [{"failure_reason": f"r{i}"} for i in range(8)]
        out = summarize_failures(items)
        assert out.count(";") == 4   # 5 items joined => 4 separators

    def test_ignores_items_without_reason(self):
        items = [{"other": 1}, {"failure_reason": "boom"}]
        assert summarize_failures(items) == "boom"


# ══════════════════════════════════════════════════════════════
# LAYER 1c — Unit: task decomposition / dependency scheduling
# ══════════════════════════════════════════════════════════════

class TestBuildImplementationPlan:
    def test_always_includes_backend_workers(self, project_id):
        plan = build_implementation_plan(project_id, "f", {})
        worker_ids = {t.worker_agent_id for t in plan.tasks}
        assert set(BACKEND_WORKERS).issubset(worker_ids)

    def test_always_includes_integration_workers(self, project_id):
        plan = build_implementation_plan(project_id, "f", {})
        worker_ids = {t.worker_agent_id for t in plan.tasks}
        assert set(INTEGRATION_WORKERS).issubset(worker_ids)

    def test_excludes_frontend_without_ui_blueprint(self, project_id):
        plan = build_implementation_plan(project_id, "f", {})
        worker_ids = {t.worker_agent_id for t in plan.tasks}
        assert not set(FRONTEND_WORKERS) & worker_ids

    def test_includes_frontend_with_ui_blueprint(self, project_id):
        plan = build_implementation_plan(project_id, "f", {"ui_blueprint": UI_BLUEPRINT})
        worker_ids = {t.worker_agent_id for t in plan.tasks}
        assert set(FRONTEND_WORKERS).issubset(worker_ids)

    def test_include_frontend_false_overrides_ui_blueprint_presence(self, project_id):
        plan = build_implementation_plan(project_id, "f", {"ui_blueprint": UI_BLUEPRINT}, include_frontend=False)
        worker_ids = {t.worker_agent_id for t in plan.tasks}
        assert not set(FRONTEND_WORKERS) & worker_ids

    def test_api_worker_depends_on_auth_and_business_logic(self, project_id):
        plan = build_implementation_plan(project_id, "f", {})
        by_worker = {t.worker_agent_id: t for t in plan.tasks}
        api_deps = set(by_worker["api_implementation_worker"].depends_on)
        assert by_worker["authentication_worker"].task_id in api_deps
        assert by_worker["business_logic_worker"].task_id in api_deps

    def test_database_worker_has_no_dependencies(self, project_id):
        plan = build_implementation_plan(project_id, "f", {})
        by_worker = {t.worker_agent_id: t for t in plan.tasks}
        assert by_worker["database_layer_worker"].depends_on == []

    def test_page_worker_depends_on_component_and_routing(self, project_id):
        plan = build_implementation_plan(project_id, "f", {"ui_blueprint": UI_BLUEPRINT})
        by_worker = {t.worker_agent_id: t for t in plan.tasks}
        page_deps = set(by_worker["page_worker"].depends_on)
        assert by_worker["component_worker"].task_id in page_deps
        assert by_worker["routing_worker"].task_id in page_deps

    def test_messaging_worker_depends_on_internal_event_worker(self, project_id):
        plan = build_implementation_plan(project_id, "f", {})
        by_worker = {t.worker_agent_id: t for t in plan.tasks}
        assert by_worker["internal_integration_worker"].task_id in by_worker["messaging_worker"].depends_on

    def test_plan_has_correct_total_task_count_without_ui(self, project_id):
        plan = build_implementation_plan(project_id, "f", {})
        assert len(plan.tasks) == len(BACKEND_WORKERS) + len(INTEGRATION_WORKERS)

    def test_plan_has_correct_total_task_count_with_ui(self, project_id):
        plan = build_implementation_plan(project_id, "f", {"ui_blueprint": UI_BLUEPRINT})
        assert len(plan.tasks) == len(BACKEND_WORKERS) + len(FRONTEND_WORKERS) + len(INTEGRATION_WORKERS)


class TestTopologicalBatches:
    def test_independent_tasks_form_single_batch(self):
        tasks = [EngineeringTask(project_id="p", team=EngineeringTeam.BACKEND, worker_agent_id=f"w{i}")
                 for i in range(3)]
        batches = topological_batches(tasks)
        assert len(batches) == 1 and len(batches[0]) == 3

    def test_chain_forms_sequential_batches(self):
        t1 = EngineeringTask(project_id="p", team=EngineeringTeam.BACKEND, worker_agent_id="a")
        t2 = EngineeringTask(project_id="p", team=EngineeringTeam.BACKEND, worker_agent_id="b", depends_on=[t1.task_id])
        t3 = EngineeringTask(project_id="p", team=EngineeringTeam.BACKEND, worker_agent_id="c", depends_on=[t2.task_id])
        batches = topological_batches([t3, t1, t2])
        assert [b[0].worker_agent_id for b in batches] == ["a", "b", "c"]

    def test_diamond_dependency_batches_correctly(self):
        t1 = EngineeringTask(project_id="p", team=EngineeringTeam.BACKEND, worker_agent_id="root")
        t2 = EngineeringTask(project_id="p", team=EngineeringTeam.BACKEND, worker_agent_id="left", depends_on=[t1.task_id])
        t3 = EngineeringTask(project_id="p", team=EngineeringTeam.BACKEND, worker_agent_id="right", depends_on=[t1.task_id])
        t4 = EngineeringTask(project_id="p", team=EngineeringTeam.BACKEND, worker_agent_id="join",
                              depends_on=[t2.task_id, t3.task_id])
        batches = topological_batches([t1, t2, t3, t4])
        assert len(batches) == 3 and len(batches[1]) == 2

    def test_cycle_raises_value_error(self):
        t1 = EngineeringTask(project_id="p", team=EngineeringTeam.BACKEND, worker_agent_id="a")
        t2 = EngineeringTask(project_id="p", team=EngineeringTeam.BACKEND, worker_agent_id="b")
        t1.depends_on = [t2.task_id]
        t2.depends_on = [t1.task_id]
        with pytest.raises(ValueError):
            topological_batches([t1, t2])


class TestTeamProgress:
    def test_counts_total_completed_failed_escalated(self):
        t1 = EngineeringTask(project_id="p", team=EngineeringTeam.BACKEND, worker_agent_id="a",
                              status=EngineeringTaskStatus.COMPLETED)
        t2 = EngineeringTask(project_id="p", team=EngineeringTeam.BACKEND, worker_agent_id="b",
                              status=EngineeringTaskStatus.FAILED, escalated=True)
        plan = ImplementationPlan(project_id="p", feature_name="f", tasks=[t1, t2])
        progress = team_progress(plan, EngineeringTeam.BACKEND)
        assert progress == {"total": 2, "completed": 1, "failed": 1, "escalated": 1}


# ══════════════════════════════════════════════════════════════
# LAYER 1d — Unit: routing predicates
# ══════════════════════════════════════════════════════════════

class TestEngineeringRouting:
    def test_route_after_plan_success(self):
        assert route_after_plan({"phase_status": "running"}) == "fan_out"

    def test_route_after_plan_failure(self):
        assert route_after_plan({"phase_status": "failed"}) == "failed"

    def test_route_after_fan_out_success(self):
        assert route_after_fan_out({"phase_status": "running", "any_dead_lettered": False}) == "aggregate"

    def test_route_after_fan_out_dlq(self):
        assert route_after_fan_out({"phase_status": "running", "any_dead_lettered": True}) == "dlq"

    def test_route_after_fan_out_failure(self):
        assert route_after_fan_out({"phase_status": "failed"}) == "failed"

    def test_route_after_aggregate_incomplete_loops_back(self):
        assert route_after_aggregate({"all_tasks_complete": False}) == "fan_out"

    def test_route_after_aggregate_complete_goes_to_review(self):
        assert route_after_aggregate({"all_tasks_complete": True}) == "review"

    def test_route_after_review_pass(self):
        assert route_after_review({"review_verdict": "pass", "review_cycles_run": 1}) == "repository"

    def test_route_after_review_revise_within_budget(self):
        assert route_after_review({"review_verdict": "revise", "review_cycles_run": 1}) == "revise"

    def test_route_after_review_revise_exhausted_fails(self):
        assert route_after_review({"review_verdict": "revise", "review_cycles_run": MAX_REVIEW_CYCLES}) == "failed"

    def test_route_after_review_block_fails(self):
        assert route_after_review({"review_verdict": "block", "review_cycles_run": 0}) == "failed"

    def test_route_after_repository_success(self):
        assert route_after_repository({"phase_status": "running"}) == "publish"

    def test_route_after_repository_failure(self):
        assert route_after_repository({"phase_status": "failed"}) == "failed"

    def test_route_task_retry_done(self):
        assert route_task_retry({"status": "completed", "retry_count": 0}) == "done"

    def test_route_task_retry_retry_within_budget(self):
        assert route_task_retry({"status": "failed", "retry_count": 1}) == "retry"

    def test_route_task_retry_dead_letter_when_exhausted(self):
        assert route_task_retry({"status": "failed", "retry_count": MAX_TASK_RETRIES}) == "dead_letter"

    def test_route_checkpoint_recovery_valid_stage(self):
        assert route_checkpoint_recovery({"resume_at_stage": "review"}) == "review"

    def test_route_checkpoint_recovery_invalid_stage_defaults_to_plan(self):
        assert route_checkpoint_recovery({"resume_at_stage": "not_a_stage"}) == "plan"

    def test_route_checkpoint_recovery_missing_stage_defaults_to_plan(self):
        assert route_checkpoint_recovery({}) == "plan"


# ══════════════════════════════════════════════════════════════
# LAYER 1e — Unit: agent registry + Appendix A patch verification
# ══════════════════════════════════════════════════════════════

class TestEngineeringRegistry:
    ALL_20 = [
        "engineering_head",
        "backend_lead", "database_layer_worker", "authentication_worker",
        "business_logic_worker", "api_implementation_worker",
        "frontend_lead", "component_worker", "page_worker",
        "state_management_worker", "routing_worker",
        "integration_lead", "internal_integration_worker",
        "third_party_integration_worker", "messaging_worker",
        "code_review_lead", "code_reviewer_worker", "refactor_worker",
        "quality_worker", "commit_worker",
    ]

    def test_all_20_engineering_agents_registered(self):
        for aid in self.ALL_20:
            assert aid in AGENT_REGISTRY, f"Missing: {aid}"

    def test_exactly_20_engineering_agents(self):
        eng = [s for s in AGENT_REGISTRY.values() if s.department == "engineering"]
        assert len(eng) == 20

    def test_department_layer_counts(self):
        eng = [s for s in AGENT_REGISTRY.values() if s.department == "engineering"]
        heads   = [s for s in eng if s.role == "head"]
        leads   = [s for s in eng if s.role == "lead"]
        workers = [s for s in eng if s.role == "worker"]
        assert len(heads) == 1 and len(leads) == 4 and len(workers) == 15

    def test_engineering_head_uses_premium_model(self):
        assert AGENT_REGISTRY["engineering_head"].default_model == "claude-opus-4-6"

    def test_engineering_head_parent_is_manager_agent(self):
        assert AGENT_REGISTRY["engineering_head"].parent_agent_id == "manager_agent"

    @pytest.mark.parametrize("worker_id,lead_id", [
        ("database_layer_worker", "backend_lead"),
        ("authentication_worker", "backend_lead"),
        ("business_logic_worker", "backend_lead"),
        ("api_implementation_worker", "backend_lead"),
        ("component_worker", "frontend_lead"),
        ("page_worker", "frontend_lead"),
        ("state_management_worker", "frontend_lead"),
        ("routing_worker", "frontend_lead"),
        ("internal_integration_worker", "integration_lead"),
        ("third_party_integration_worker", "integration_lead"),
        ("messaging_worker", "integration_lead"),
        ("code_reviewer_worker", "code_review_lead"),
        ("refactor_worker", "code_review_lead"),
        ("quality_worker", "code_review_lead"),
        ("commit_worker", "code_review_lead"),
    ])
    def test_worker_parent_lead_mapping(self, worker_id, lead_id):
        assert AGENT_REGISTRY[worker_id].parent_agent_id == lead_id

    def test_new_m33_workers_are_flagged_correctly(self):
        for new_worker in ["routing_worker", "messaging_worker", "quality_worker", "commit_worker"]:
            assert AGENT_REGISTRY[new_worker].role == "worker"
            assert AGENT_REGISTRY[new_worker].layer == 5

    def test_no_agent_ids_were_renamed_from_original_16(self):
        # Every agent_id present before M3.3 must still resolve — only *labels* changed.
        original_16 = [
            "engineering_head", "backend_lead", "api_implementation_worker",
            "database_layer_worker", "authentication_worker", "business_logic_worker",
            "frontend_lead", "component_worker", "page_worker", "state_management_worker",
            "integration_lead", "third_party_integration_worker", "internal_integration_worker",
            "code_review_lead", "code_reviewer_worker", "refactor_worker",
        ]
        for aid in original_16:
            assert aid in AGENT_REGISTRY


class TestAppendixAPatch:
    def test_ui_architect_worker_registered(self):
        assert "ui_architect_worker" in AGENT_REGISTRY

    def test_ui_architect_worker_parent_is_platform_design_lead(self):
        assert AGENT_REGISTRY["ui_architect_worker"].parent_agent_id == "platform_design_lead"

    def test_ui_architect_worker_is_architecture_department(self):
        assert AGENT_REGISTRY["ui_architect_worker"].department == "architecture"

    def test_ui_blueprint_in_architecture_approval_gate(self):
        from services.architecture.head import APPROVAL_ARTIFACTS
        assert "ui_blueprint" in APPROVAL_ARTIFACTS

    def test_platform_design_lead_includes_ui_worker(self):
        from services.architecture.leads import PlatformDesignLead
        worker_ids = [w for w, _ in PlatformDesignLead.WORKERS]
        assert "ui_architect_worker" in worker_ids

    def test_branch_type_integration_exists(self):
        assert BranchType.INTEGRATION.value == "integration"

    def test_build_branch_name_integration_format(self):
        name = build_branch_name(branch_type="integration", task_id=None, incident_id=None, slug="user-auth")
        assert name == "integration/user-auth"

    def test_build_branch_name_integration_requires_slug(self):
        with pytest.raises(InvalidBranchNameError):
            build_branch_name(branch_type="integration", task_id=None, incident_id=None, slug=None)

    def test_build_branch_name_feature_unaffected_by_patch(self):
        name = build_branch_name(branch_type="feature", task_id="T-1", incident_id=None, slug="add-login")
        assert name == "feature/T-1-add-login"

    def test_build_branch_name_hotfix_unaffected_by_patch(self):
        name = build_branch_name(branch_type="hotfix", task_id=None, incident_id="INC-1", slug=None)
        assert name == "hotfix/INC-1"


# ══════════════════════════════════════════════════════════════
# LAYER 2 — Graph: node functions + graph construction
# ══════════════════════════════════════════════════════════════

class TestEngineeringGraph:
    def _base_state(self, project_id="p") -> Dict[str, Any]:
        return {
            "project_id": project_id, "workflow_id": "wf-1", "feature_name": "f",
            "plan_ready": False, "total_tasks": 0,
            "backend_ready": False, "frontend_ready": False, "frontend_skipped": False,
            "integration_ready": False, "all_tasks_complete": False, "modules_aggregated": 0,
            "review_verdict": "pass", "review_cycles_run": 0,
            "any_dead_lettered": False, "dlq_tasks": [],
            "integration_branch": None, "pull_request_id": None, "merge_sha": None,
            "phase_status": "running", "failure_reason": None, "resume_at_stage": None,
            "nats_events_queue": [], "ws_events_queue": [],
        }

    def test_graph_builds_without_error(self):
        from services.engineering.workflows.engineering_graph import build_engineering_graph
        assert build_engineering_graph() is not None

    def test_graph_builds_with_checkpointer_kwarg_path(self):
        from services.engineering.workflows.engineering_graph import build_engineering_graph
        assert build_engineering_graph(checkpointer=None) is not None

    @pytest.mark.asyncio
    async def test_implementation_plan_node_sets_ready(self):
        from services.engineering.workflows.engineering_graph import implementation_plan_node
        r = await implementation_plan_node(self._base_state())
        assert r["plan_ready"] is True and r["phase_status"] == "running"

    @pytest.mark.asyncio
    async def test_task_breakdown_node_publishes_event(self):
        from services.engineering.workflows.engineering_graph import task_breakdown_node
        r = await task_breakdown_node(self._base_state())
        subjects = [e["subject"] for e in r["nats_events_queue"]]
        assert "engineering.plan.created" in subjects

    @pytest.mark.asyncio
    async def test_fan_out_backend_node(self):
        from services.engineering.workflows.engineering_graph import fan_out_backend_node
        r = await fan_out_backend_node(self._base_state())
        assert r["backend_ready"] is True

    @pytest.mark.asyncio
    async def test_fan_out_frontend_node(self):
        from services.engineering.workflows.engineering_graph import fan_out_frontend_node
        r = await fan_out_frontend_node(self._base_state())
        assert r["frontend_ready"] is True

    @pytest.mark.asyncio
    async def test_fan_out_integration_node(self):
        from services.engineering.workflows.engineering_graph import fan_out_integration_node
        r = await fan_out_integration_node(self._base_state())
        assert r["integration_ready"] is True

    @pytest.mark.asyncio
    async def test_aggregate_results_all_ready(self):
        from services.engineering.workflows.engineering_graph import aggregate_results_node
        s = self._base_state()
        s.update(backend_ready=True, frontend_ready=True, integration_ready=True)
        r = await aggregate_results_node(s)
        assert r["all_tasks_complete"] is True

    @pytest.mark.asyncio
    async def test_aggregate_results_frontend_skipped_still_ready(self):
        from services.engineering.workflows.engineering_graph import aggregate_results_node
        s = self._base_state()
        s.update(backend_ready=True, frontend_ready=False, frontend_skipped=True, integration_ready=True)
        r = await aggregate_results_node(s)
        assert r["all_tasks_complete"] is True

    @pytest.mark.asyncio
    async def test_aggregate_results_missing_team_fails(self):
        from services.engineering.workflows.engineering_graph import aggregate_results_node
        s = self._base_state()
        s.update(backend_ready=True, frontend_ready=False, frontend_skipped=False, integration_ready=True)
        r = await aggregate_results_node(s)
        assert r["all_tasks_complete"] is False and r["phase_status"] == "failed"

    @pytest.mark.asyncio
    async def test_review_cycle_node_increments_counter(self):
        from services.engineering.workflows.engineering_graph import review_cycle_node
        s = self._base_state(); s["review_cycles_run"] = 1
        r = await review_cycle_node(s)
        assert r["review_cycles_run"] == 2

    @pytest.mark.asyncio
    async def test_repository_node_publishes_push_started(self):
        from services.engineering.workflows.engineering_graph import repository_node
        r = await repository_node(self._base_state())
        subjects = [e["subject"] for e in r["nats_events_queue"]]
        assert "engineering.repository.push_started" in subjects

    @pytest.mark.asyncio
    async def test_dlq_node_sets_failed_with_reason(self):
        from services.engineering.workflows.engineering_graph import dlq_node
        s = self._base_state(); s["dlq_tasks"] = ["t1", "t2"]
        r = await dlq_node(s)
        assert r["phase_status"] == "failed" and "t1" in r["failure_reason"]

    @pytest.mark.asyncio
    async def test_publish_artifacts_node_completes_phase(self):
        from services.engineering.workflows.engineering_graph import publish_artifacts_node
        r = await publish_artifacts_node(self._base_state())
        subjects = [e["subject"] for e in r["nats_events_queue"]]
        assert r["phase_status"] == "completed"
        assert "engineering.phase.completed" in subjects

    @pytest.mark.asyncio
    async def test_handle_failure_node_publishes_failure_event(self):
        from services.engineering.workflows.engineering_graph import handle_failure_node
        s = self._base_state(); s["failure_reason"] = "boom"
        r = await handle_failure_node(s)
        subjects = [e["subject"] for e in r["nats_events_queue"]]
        assert "engineering.pipeline.failed" in subjects


# ══════════════════════════════════════════════════════════════
# LAYER 3a — Integration: Repository Service client (mocked httpx)
# ══════════════════════════════════════════════════════════════

class TestRepositoryServiceClient:
    def _mock_client(self, json_body: Dict[str, Any], status: int = 200):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = json_body
        resp.text = json.dumps(json_body)

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        return mock_client

    @pytest.mark.asyncio
    async def test_create_integration_branch_calls_correct_path(self):
        client = RepositoryServiceClient(base_url="http://test")
        mock_client = self._mock_client({"name": "integration/f"})
        with patch("services.engineering.integration.repository_client.httpx.AsyncClient", return_value=mock_client):
            result = await client.create_integration_branch("p1", "f")
        mock_client.request.assert_called_once()
        args, kwargs = mock_client.request.call_args
        assert args[0] == "POST" and args[1] == "/branches"
        assert kwargs["json"]["branch_type"] == "integration"
        assert kwargs["json"]["slug"] == "f"
        assert result["name"] == "integration/f"

    @pytest.mark.asyncio
    async def test_commit_files_sends_expected_payload(self):
        client = RepositoryServiceClient(base_url="http://test")
        mock_client = self._mock_client({"sha": "abc123"})
        with patch("services.engineering.integration.repository_client.httpx.AsyncClient", return_value=mock_client):
            result = await client.commit_files("p1", "integration/f", "feat: x",
                                                 [{"path": "a.py", "content": "x"}], {"task_id": "t1"})
        args, kwargs = mock_client.request.call_args
        assert args[1] == "/commits"
        assert kwargs["json"]["branch_name"] == "integration/f"
        assert result["sha"] == "abc123"

    @pytest.mark.asyncio
    async def test_create_pull_request_sends_expected_payload(self):
        client = RepositoryServiceClient(base_url="http://test")
        mock_client = self._mock_client({"id": "pr-1", "html_url": "http://x/pr/1"})
        with patch("services.engineering.integration.repository_client.httpx.AsyncClient", return_value=mock_client):
            result = await client.create_pull_request("p1", "integration/f", "Engineering: f")
        args, kwargs = mock_client.request.call_args
        assert args[1] == "/pull-requests"
        assert kwargs["json"]["source_branch"] == "integration/f"
        assert result["id"] == "pr-1"

    @pytest.mark.asyncio
    async def test_approve_pull_request_sends_expected_payload(self):
        client = RepositoryServiceClient(base_url="http://test")
        mock_client = self._mock_client({"status": "approved"})
        with patch("services.engineering.integration.repository_client.httpx.AsyncClient", return_value=mock_client):
            await client.approve_pull_request("p1", "pr-1", "reviewer-1")
        args, kwargs = mock_client.request.call_args
        assert args[1] == "/pull-requests/approve"
        assert kwargs["json"]["approved_by"] == "reviewer-1"

    @pytest.mark.asyncio
    async def test_merge_pull_request_sends_expected_payload(self):
        client = RepositoryServiceClient(base_url="http://test")
        mock_client = self._mock_client({"status": "merged", "merge_sha": "deadbeef"})
        with patch("services.engineering.integration.repository_client.httpx.AsyncClient", return_value=mock_client):
            result = await client.merge_pull_request("p1", "pr-1")
        args, kwargs = mock_client.request.call_args
        assert args[1] == "/pull-requests/merge"
        assert "strategy" not in kwargs["json"]   # squash is server-enforced, not client-selectable
        assert result["merge_sha"] == "deadbeef"

    @pytest.mark.asyncio
    async def test_error_response_raises_client_error_with_status(self):
        client = RepositoryServiceClient(base_url="http://test")
        mock_client = self._mock_client({"detail": "conflict"}, status=409)
        with patch("services.engineering.integration.repository_client.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(RepositoryServiceClientError) as exc_info:
                await client.merge_pull_request("p1", "pr-1")
        assert exc_info.value.status_code == 409
        assert exc_info.value.path == "/pull-requests/merge"

    def test_default_base_url_uses_settings_port(self):
        client = RepositoryServiceClient()
        assert "8006" in client._base_url


# ══════════════════════════════════════════════════════════════
# LAYER 3b — Integration: Backend team workers (mocked LLM)
# ══════════════════════════════════════════════════════════════

MOCK_DB_FILES = json.dumps({"files": [{"path": "app/models/user.py", "language": "python", "content": "class User: pass"}],
                            "tables_implemented": ["users"], "quality_score": 0.9})
MOCK_AUTH_FILES = json.dumps({"files": [{"path": "app/auth/jwt.py", "language": "python", "content": "def create_token(): pass"}],
                              "features_implemented": ["login"], "quality_score": 0.92})
MOCK_BIZ_FILES = json.dumps({"files": [{"path": "app/services/project_service.py", "language": "python", "content": "class ProjectService: pass"}],
                             "quality_score": 0.88})
MOCK_API_FILES = json.dumps({"files": [{"path": "app/routers/health.py", "language": "python", "content": "router = None"}],
                             "modules_implemented": ["health"], "quality_score": 0.87})
MOCK_CRITIQUE_PASS = json.dumps({"passed": True, "score": 0.9, "blocking": [], "warnings": [], "suggestions": []})
MOCK_CRITIQUE_FAIL = json.dumps({"passed": False, "score": 0.4, "blocking": ["insecure token storage"], "warnings": [], "suggestions": ["use httpOnly cookies"]})


class TestBackendWorkers:
    @pytest.mark.asyncio
    async def test_database_layer_worker_generates_module(self, eng_task):
        from services.engineering.workers.backend import DatabaseLayerWorker
        infra = make_infra()
        agent = inject(DatabaseLayerWorker, infra, "database_layer_worker")
        p1, p2, p3 = patched(agent, MOCK_DB_FILES)
        with p1, p2, p3:
            result = await agent.execute(eng_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["module_type"] == "database"
        assert len(result.artifacts) == 1

    @pytest.mark.asyncio
    async def test_authentication_worker_generates_module(self, eng_task):
        from services.engineering.workers.backend import AuthenticationWorker
        infra = make_infra()
        agent = inject(AuthenticationWorker, infra, "authentication_worker")
        p1, p2, p3 = patched(agent, MOCK_AUTH_FILES)
        with p1, p2, p3:
            result = await agent.execute(eng_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["module_type"] == "auth"

    @pytest.mark.asyncio
    async def test_authentication_worker_escalates_on_review_failure(self, eng_task):
        from services.engineering.workers.backend import AuthenticationWorker
        infra = make_infra()
        agent = inject(AuthenticationWorker, infra, "authentication_worker")
        # Auth is security-critical: force the ReviewCycle's internal critique to fail every cycle.
        with patch.object(agent, "_pre_execute", AsyncMock()), \
             patch.object(agent, "_post_execute", AsyncMock()), \
             patch.object(agent, "call_llm", AsyncMock(side_effect=[
                 (MOCK_AUTH_FILES, None),
                 (MOCK_CRITIQUE_FAIL, None), (MOCK_AUTH_FILES, None),
                 (MOCK_CRITIQUE_FAIL, None), (MOCK_AUTH_FILES, None),
                 (MOCK_CRITIQUE_FAIL, None), (MOCK_AUTH_FILES, None),
             ])):
            result = await agent.execute(eng_task)
        assert result.status == TaskStatus.ESCALATED

    @pytest.mark.asyncio
    async def test_business_logic_worker_generates_module(self, eng_task):
        from services.engineering.workers.backend import BusinessLogicWorker
        infra = make_infra()
        agent = inject(BusinessLogicWorker, infra, "business_logic_worker")
        p1, p2, p3 = patched(agent, MOCK_BIZ_FILES)
        with p1, p2, p3:
            result = await agent.execute(eng_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["module_type"] == "business_logic"

    @pytest.mark.asyncio
    async def test_api_implementation_worker_generates_module(self, eng_task):
        from services.engineering.workers.backend import ApiImplementationWorker
        infra = make_infra()
        agent = inject(ApiImplementationWorker, infra, "api_implementation_worker")
        p1, p2, p3 = patched(agent, MOCK_API_FILES)
        with p1, p2, p3:
            result = await agent.execute(eng_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["module_type"] == "api_endpoint"

    @pytest.mark.asyncio
    async def test_backend_module_includes_idempotent_key(self, eng_task):
        from services.engineering.workers.backend import DatabaseLayerWorker
        infra = make_infra()
        agent = inject(DatabaseLayerWorker, infra, "database_layer_worker")
        p1, p2, p3 = patched(agent, MOCK_DB_FILES)
        with p1, p2, p3:
            result = await agent.execute(eng_task)
        assert result.content.get("idempotent_key")


# ══════════════════════════════════════════════════════════════
# LAYER 3c — Integration: Frontend team workers (require ui_blueprint)
# ══════════════════════════════════════════════════════════════

MOCK_COMPONENT_FILES = json.dumps({"files": [{"path": "src/components/NavBar.tsx", "language": "typescript", "content": "export const NavBar = () => null;"}],
                                    "components_implemented": ["NavBar"], "quality_score": 0.87})
MOCK_STATE_FILES = json.dumps({"files": [{"path": "src/store/authStore.ts", "language": "typescript", "content": "export const useAuthStore = () => ({});"}],
                               "quality_score": 0.86})
MOCK_ROUTING_FILES = json.dumps({"files": [{"path": "src/router/index.tsx", "language": "typescript", "content": "export const routes = [];"}],
                                 "quality_score": 0.86})
MOCK_PAGE_FILES = json.dumps({"files": [{"path": "src/pages/Dashboard.tsx", "language": "typescript", "content": "export default function Dashboard() { return null; }"}],
                              "quality_score": 0.87})


class TestFrontendWorkers:
    @pytest.mark.asyncio
    async def test_component_worker_escalates_without_ui_blueprint(self, eng_task_no_ui):
        from services.engineering.workers.frontend import ComponentWorker
        infra = make_infra()
        agent = inject(ComponentWorker, infra, "component_worker")
        result = await agent.execute(eng_task_no_ui)
        assert result.status == TaskStatus.ESCALATED

    @pytest.mark.asyncio
    async def test_component_worker_generates_with_ui_blueprint(self, eng_task):
        from services.engineering.workers.frontend import ComponentWorker
        infra = make_infra()
        agent = inject(ComponentWorker, infra, "component_worker")
        p1, p2, p3 = patched(agent, MOCK_COMPONENT_FILES)
        with p1, p2, p3:
            result = await agent.execute(eng_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["module_type"] == "component"

    @pytest.mark.asyncio
    async def test_state_management_worker_escalates_without_ui_blueprint(self, eng_task_no_ui):
        from services.engineering.workers.frontend import StateManagementWorker
        infra = make_infra()
        agent = inject(StateManagementWorker, infra, "state_management_worker")
        result = await agent.execute(eng_task_no_ui)
        assert result.status == TaskStatus.ESCALATED

    @pytest.mark.asyncio
    async def test_state_management_worker_generates_with_ui_blueprint(self, eng_task):
        from services.engineering.workers.frontend import StateManagementWorker
        infra = make_infra()
        agent = inject(StateManagementWorker, infra, "state_management_worker")
        p1, p2, p3 = patched(agent, MOCK_STATE_FILES)
        with p1, p2, p3:
            result = await agent.execute(eng_task)
        assert result.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_routing_worker_escalates_without_ui_blueprint(self, eng_task_no_ui):
        from services.engineering.workers.frontend import RoutingWorker
        infra = make_infra()
        agent = inject(RoutingWorker, infra, "routing_worker")
        result = await agent.execute(eng_task_no_ui)
        assert result.status == TaskStatus.ESCALATED

    @pytest.mark.asyncio
    async def test_routing_worker_generates_with_ui_blueprint(self, eng_task):
        from services.engineering.workers.frontend import RoutingWorker
        infra = make_infra()
        agent = inject(RoutingWorker, infra, "routing_worker")
        p1, p2, p3 = patched(agent, MOCK_ROUTING_FILES)
        with p1, p2, p3:
            result = await agent.execute(eng_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["module_type"] == "routing"

    @pytest.mark.asyncio
    async def test_page_worker_escalates_without_ui_blueprint(self, eng_task_no_ui):
        from services.engineering.workers.frontend import PageWorker
        infra = make_infra()
        agent = inject(PageWorker, infra, "page_worker")
        result = await agent.execute(eng_task_no_ui)
        assert result.status == TaskStatus.ESCALATED

    @pytest.mark.asyncio
    async def test_page_worker_generates_with_ui_blueprint(self, eng_task):
        from services.engineering.workers.frontend import PageWorker
        infra = make_infra()
        agent = inject(PageWorker, infra, "page_worker")
        p1, p2, p3 = patched(agent, MOCK_PAGE_FILES)
        with p1, p2, p3:
            result = await agent.execute(eng_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["module_type"] == "page"


# ══════════════════════════════════════════════════════════════
# LAYER 3d — Integration: Integration team workers
# ══════════════════════════════════════════════════════════════

MOCK_EVENT_FILES = json.dumps({"files": [{"path": "app/events/handlers.py", "language": "python", "content": "async def on_x(): pass"}],
                               "events_implemented": ["project.created"], "quality_score": 0.87})
MOCK_EXTERNAL_FILES = json.dumps({"files": [{"path": "app/integrations/email.py", "language": "python", "content": "async def send(): pass"}],
                                  "integrations_implemented": ["email"], "quality_score": 0.85})
MOCK_MESSAGING_FILES = json.dumps({"files": [{"path": "app/messaging/publisher.py", "language": "python", "content": "async def publish(): pass"}],
                                   "subjects_implemented": ["project.created"], "quality_score": 0.86})


class TestIntegrationWorkers:
    @pytest.mark.asyncio
    async def test_internal_integration_worker_generates_module(self, eng_task):
        from services.engineering.workers.integration import InternalIntegrationWorker
        infra = make_infra()
        agent = inject(InternalIntegrationWorker, infra, "internal_integration_worker")
        p1, p2, p3 = patched(agent, MOCK_EVENT_FILES)
        with p1, p2, p3:
            result = await agent.execute(eng_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["module_type"] == "internal_event"

    @pytest.mark.asyncio
    async def test_third_party_integration_worker_generates_module(self, eng_task):
        from services.engineering.workers.integration import ThirdPartyIntegrationWorker
        infra = make_infra()
        agent = inject(ThirdPartyIntegrationWorker, infra, "third_party_integration_worker")
        p1, p2, p3 = patched(agent, MOCK_EXTERNAL_FILES)
        with p1, p2, p3:
            result = await agent.execute(eng_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["module_type"] == "external_api"

    @pytest.mark.asyncio
    async def test_messaging_worker_generates_module(self, eng_task):
        from services.engineering.workers.integration import MessagingWorker
        infra = make_infra()
        agent = inject(MessagingWorker, infra, "messaging_worker")
        p1, p2, p3 = patched(agent, MOCK_MESSAGING_FILES)
        with p1, p2, p3:
            result = await agent.execute(eng_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["module_type"] == "messaging"

    @pytest.mark.asyncio
    async def test_messaging_worker_reads_internal_events_from_context(self, eng_task):
        from services.engineering.workers.integration import MessagingWorker
        eng_task.context.approved_artifacts["internal_integration_worker"] = {
            "events_implemented": ["project.created", "task.completed"]}
        infra = make_infra()
        agent = inject(MessagingWorker, infra, "messaging_worker")
        p1, p2, p3 = patched(agent, MOCK_MESSAGING_FILES)
        with p1, p2, p3:
            result = await agent.execute(eng_task)
        assert result.status == TaskStatus.COMPLETED


# ══════════════════════════════════════════════════════════════
# LAYER 3e — Integration: Review team workers
# ══════════════════════════════════════════════════════════════

MOCK_REVIEW_PASS = json.dumps({"review_passed": True, "issues": [], "compliance_score": 0.9,
                               "security_flags": [], "quality_score": 0.9})
MOCK_REVIEW_FAIL = json.dumps({"review_passed": False,
                               "issues": [{"severity": "blocking", "file": "a.py", "line": 1, "description": "SQL injection risk"}],
                               "compliance_score": 0.4, "quality_score": 0.4})
MOCK_REFACTOR = json.dumps({"files_modified": [{"path": "a.py", "change": "parameterized query"}],
                            "issues_fixed": 1, "quality_score": 0.9})


class TestReviewWorkers:
    @pytest.mark.asyncio
    async def test_code_reviewer_worker_passes_clean_code(self, eng_task):
        from services.engineering.workers.review import CodeReviewerWorker
        eng_task.context.approved_artifacts["__current_module__"] = {"files": [{"path": "a.py"}]}
        infra = make_infra()
        agent = inject(CodeReviewerWorker, infra, "code_reviewer_worker")
        p1, p2, p3 = patched(agent, MOCK_REVIEW_PASS)
        with p1, p2, p3:
            result = await agent.execute(eng_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["verdict"] == "pass"

    @pytest.mark.asyncio
    async def test_code_reviewer_worker_fails_blocking_issue(self, eng_task):
        from services.engineering.workers.review import CodeReviewerWorker
        eng_task.context.approved_artifacts["__current_module__"] = {"files": [{"path": "a.py"}]}
        infra = make_infra()
        agent = inject(CodeReviewerWorker, infra, "code_reviewer_worker")
        p1, p2, p3 = patched(agent, MOCK_REVIEW_FAIL)
        with p1, p2, p3:
            result = await agent.execute(eng_task)
        assert result.status == TaskStatus.FAILED
        assert result.failure_reason and "blocking" in result.failure_reason

    @pytest.mark.asyncio
    async def test_refactor_worker_applies_fixes(self, eng_task):
        from services.engineering.workers.review import RefactorWorker
        eng_task.context.approved_artifacts["__review_feedback__"] = json.loads(MOCK_REVIEW_FAIL)
        infra = make_infra()
        agent = inject(RefactorWorker, infra, "refactor_worker")
        p1, p2, p3 = patched(agent, MOCK_REFACTOR)
        with p1, p2, p3:
            result = await agent.execute(eng_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["issues_fixed"] == 1

    @pytest.mark.asyncio
    async def test_quality_worker_passes_complete_module(self, eng_task):
        from services.engineering.workers.review import QualityWorker
        eng_task.context.approved_artifacts["__current_module__"] = {
            "files": [{"path": "a.py", "content": "print(1)"}],
            "quality_score": 0.9, "idempotent_key": "abc123",
        }
        infra = make_infra()
        agent = inject(QualityWorker, infra, "quality_worker")
        result = await agent.execute(eng_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["coding_contract_satisfied"] is True

    @pytest.mark.asyncio
    async def test_quality_worker_flags_missing_idempotent_key(self, eng_task):
        from services.engineering.workers.review import QualityWorker
        eng_task.context.approved_artifacts["__current_module__"] = {
            "files": [{"path": "a.py", "content": "print(1)"}], "quality_score": 0.9,
        }
        infra = make_infra()
        agent = inject(QualityWorker, infra, "quality_worker")
        result = await agent.execute(eng_task)
        assert result.status == TaskStatus.FAILED
        assert "idempotent" in result.content["violations"]

    @pytest.mark.asyncio
    async def test_quality_worker_flags_empty_file_content(self, eng_task):
        from services.engineering.workers.review import QualityWorker
        eng_task.context.approved_artifacts["__current_module__"] = {
            "files": [{"path": "a.py", "content": "   "}], "quality_score": 0.9, "idempotent_key": "abc",
        }
        infra = make_infra()
        agent = inject(QualityWorker, infra, "quality_worker")
        result = await agent.execute(eng_task)
        assert "runnable" in result.content["violations"]

    @pytest.mark.asyncio
    async def test_commit_worker_escalates_with_no_modules(self, eng_task):
        from services.engineering.workers.review import CommitWorker
        infra = make_infra()
        agent = inject(CommitWorker, infra, "commit_worker")
        agent._repo_client = AsyncMock()
        result = await agent.execute(eng_task)
        assert result.status == TaskStatus.ESCALATED

    @pytest.mark.asyncio
    async def test_commit_worker_happy_path(self, eng_task):
        from services.engineering.workers.review import CommitWorker
        eng_task.context.approved_artifacts["__feature_name__"] = "user-auth"
        eng_task.context.approved_artifacts["__reviewed_modules__"] = [
            {"module_id": "m1", "module_type": "auth", "generated_by": "authentication_worker",
             "files": [{"path": "app/auth/jwt.py", "content": "x"}]},
        ]
        infra = make_infra()
        agent = inject(CommitWorker, infra, "commit_worker")
        agent._repo_client = AsyncMock()
        agent._repo_client.create_integration_branch = AsyncMock(return_value={"name": "integration/user-auth"})
        agent._repo_client.commit_files = AsyncMock(return_value={"sha": "abc123"})
        agent._repo_client.create_pull_request = AsyncMock(return_value={"id": "pr-1", "html_url": "http://x/pr/1"})
        result = await agent.execute(eng_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["integration_branch"] == "integration/user-auth"
        assert result.content["pull_request_id"] == "pr-1"
        assert result.content["commit_shas"] == ["abc123"]

    @pytest.mark.asyncio
    async def test_commit_worker_escalates_on_repository_error(self, eng_task):
        from services.engineering.workers.review import CommitWorker
        eng_task.context.approved_artifacts["__feature_name__"] = "user-auth"
        eng_task.context.approved_artifacts["__reviewed_modules__"] = [
            {"module_id": "m1", "module_type": "auth", "generated_by": "authentication_worker",
             "files": [{"path": "app/auth/jwt.py", "content": "x"}]},
        ]
        infra = make_infra()
        agent = inject(CommitWorker, infra, "commit_worker")
        agent._repo_client = AsyncMock()
        agent._repo_client.create_integration_branch = AsyncMock(
            side_effect=RepositoryServiceClientError("/branches", 503, "unavailable"))
        result = await agent.execute(eng_task)
        assert result.status == TaskStatus.ESCALATED


# ══════════════════════════════════════════════════════════════
# LAYER 3f — Integration: Leads (fake factory)
# ══════════════════════════════════════════════════════════════

class TestBackendLead:
    @pytest.mark.asyncio
    async def test_backend_lead_success(self, eng_task):
        from services.engineering.leads import BackendLead
        infra = make_infra()
        lead = inject(BackendLead, infra, "backend_lead")
        factory = FakeFactory({w: ok_result(w) for w in BACKEND_WORKERS})
        eng_task.context.approved_artifacts["__factory__"] = factory
        result = await lead.execute(eng_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["modules"] == len(BACKEND_WORKERS)

    @pytest.mark.asyncio
    async def test_backend_lead_escalates_on_worker_failure(self, eng_task):
        from services.engineering.leads import BackendLead
        infra = make_infra()
        lead = inject(BackendLead, infra, "backend_lead")
        results = {w: ok_result(w) for w in BACKEND_WORKERS}
        results["authentication_worker"] = fail_result("authentication_worker", "review failed")
        factory = FakeFactory(results)
        eng_task.context.approved_artifacts["__factory__"] = factory
        result = await lead.execute(eng_task)
        assert result.status == TaskStatus.ESCALATED


class TestFrontendLead:
    @pytest.mark.asyncio
    async def test_frontend_lead_skips_without_ui_blueprint(self, eng_task_no_ui):
        from services.engineering.leads import FrontendLead
        infra = make_infra()
        lead = inject(FrontendLead, infra, "frontend_lead")
        result = await lead.execute(eng_task_no_ui)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["skipped"] is True

    @pytest.mark.asyncio
    async def test_frontend_lead_runs_with_ui_blueprint(self, eng_task):
        from services.engineering.leads import FrontendLead
        infra = make_infra()
        lead = inject(FrontendLead, infra, "frontend_lead")
        factory = FakeFactory({w: ok_result(w) for w in FRONTEND_WORKERS})
        eng_task.context.approved_artifacts["__factory__"] = factory
        result = await lead.execute(eng_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["modules"] == len(FRONTEND_WORKERS)

    @pytest.mark.asyncio
    async def test_frontend_lead_escalates_on_worker_failure(self, eng_task):
        from services.engineering.leads import FrontendLead
        infra = make_infra()
        lead = inject(FrontendLead, infra, "frontend_lead")
        results = {w: ok_result(w) for w in FRONTEND_WORKERS}
        results["page_worker"] = fail_result("page_worker", "no ui_blueprint")
        factory = FakeFactory(results)
        eng_task.context.approved_artifacts["__factory__"] = factory
        result = await lead.execute(eng_task)
        assert result.status == TaskStatus.ESCALATED


class TestIntegrationLead:
    @pytest.mark.asyncio
    async def test_integration_lead_success(self, eng_task):
        from services.engineering.leads import IntegrationLead
        infra = make_infra()
        lead = inject(IntegrationLead, infra, "integration_lead")
        factory = FakeFactory({w: ok_result(w) for w in INTEGRATION_WORKERS})
        eng_task.context.approved_artifacts["__factory__"] = factory
        result = await lead.execute(eng_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["modules"] == len(INTEGRATION_WORKERS)

    @pytest.mark.asyncio
    async def test_integration_lead_tolerates_partial_failure(self, eng_task):
        from services.engineering.leads import IntegrationLead
        infra = make_infra()
        lead = inject(IntegrationLead, infra, "integration_lead")
        results = {w: ok_result(w) for w in INTEGRATION_WORKERS}
        results["third_party_integration_worker"] = fail_result("third_party_integration_worker", "provider down")
        factory = FakeFactory(results)
        eng_task.context.approved_artifacts["__factory__"] = factory
        result = await lead.execute(eng_task)
        assert result.status == TaskStatus.COMPLETED   # independent modules — partial success accepted
        assert result.content["failures"]


class TestReviewLead:
    @pytest.mark.asyncio
    async def test_review_lead_no_pending_modules(self, eng_task):
        from services.engineering.leads import ReviewLead
        infra = make_infra()
        lead = inject(ReviewLead, infra, "code_review_lead")
        eng_task.context.approved_artifacts["__factory__"] = FakeFactory({})
        result = await lead.execute(eng_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["reviewed"] == 0

    @pytest.mark.asyncio
    async def test_review_lead_happy_path_calls_commit_worker(self, eng_task):
        from services.engineering.leads import ReviewLead
        infra = make_infra()
        lead = inject(ReviewLead, infra, "code_review_lead")
        eng_task.context.approved_artifacts["__pending_modules__"] = [
            {"module_id": "m1", "files": [{"path": "a.py"}], "quality_score": 0.9,
             "idempotent_key": "k1", "generated_by": "database_layer_worker"},
        ]
        results = {
            "code_reviewer_worker": ok_result("code_reviewer_worker", review_passed=True),
            "quality_worker": ok_result("quality_worker", coding_contract_satisfied=True),
            "commit_worker": ok_result("commit_worker", pull_request_id="pr-1"),
        }
        factory = FakeFactory(results)
        eng_task.context.approved_artifacts["__factory__"] = factory
        result = await lead.execute(eng_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["reviewed"] == 1
        assert result.content.get("pull_request_id") == "pr-1"

    @pytest.mark.asyncio
    async def test_review_lead_blocks_module_after_max_cycles(self, eng_task):
        from services.engineering.leads import ReviewLead
        infra = make_infra()
        lead = inject(ReviewLead, infra, "code_review_lead")
        eng_task.context.approved_artifacts["__pending_modules__"] = [
            {"module_id": "m1", "files": [{"path": "a.py"}], "quality_score": 0.4,
             "generated_by": "authentication_worker"},
        ]
        results = {
            "code_reviewer_worker": ok_result("code_reviewer_worker", review_passed=False),
            "refactor_worker": ok_result("refactor_worker", files_modified=[]),
        }
        factory = FakeFactory(results)
        eng_task.context.approved_artifacts["__factory__"] = factory
        result = await lead.execute(eng_task)
        assert result.status == TaskStatus.ESCALATED

    @pytest.mark.asyncio
    async def test_review_lead_escalates_when_commit_worker_fails(self, eng_task):
        from services.engineering.leads import ReviewLead
        infra = make_infra()
        lead = inject(ReviewLead, infra, "code_review_lead")
        eng_task.context.approved_artifacts["__pending_modules__"] = [
            {"module_id": "m1", "files": [{"path": "a.py"}], "quality_score": 0.9,
             "idempotent_key": "k1", "generated_by": "database_layer_worker"},
        ]
        results = {
            "code_reviewer_worker": ok_result("code_reviewer_worker", review_passed=True),
            "quality_worker": ok_result("quality_worker", coding_contract_satisfied=True),
            "commit_worker": fail_result("commit_worker", "repository unavailable"),
        }
        factory = FakeFactory(results)
        eng_task.context.approved_artifacts["__factory__"] = factory
        result = await lead.execute(eng_task)
        assert result.status == TaskStatus.ESCALATED


# ══════════════════════════════════════════════════════════════
# LAYER 4 — E2E: EngineeringHead full pipeline (fake factory chain)
# ══════════════════════════════════════════════════════════════

@pytest.mark.e2e
class TestEngineeringHeadPipeline:
    def _full_success_factory(self) -> FakeFactory:
        return FakeFactory({
            "backend_lead": ok_result("backend_lead", team="backend", modules=4),
            "frontend_lead": ok_result("frontend_lead", team="frontend", modules=4),
            "integration_lead": ok_result("integration_lead", team="integration", modules=3),
            "code_review_lead": ok_result("code_review_lead", team="review", reviewed=11,
                                           pull_request_id="pr-42", integration_branch="integration/f"),
        })

    @pytest.mark.asyncio
    async def test_full_pipeline_completes_successfully(self, eng_task):
        from services.engineering.head import EngineeringHead
        infra = make_infra()
        head = inject(EngineeringHead, infra, "engineering_head")
        eng_task.context.approved_artifacts["__factory__"] = self._full_success_factory()
        result = await head.execute(eng_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["status"] == "complete"
        assert any(e.subject == "engineering.phase.completed" for e in result.nats_events)

    @pytest.mark.asyncio
    async def test_pipeline_escalates_on_backend_failure(self, eng_task):
        from services.engineering.head import EngineeringHead
        infra = make_infra()
        head = inject(EngineeringHead, infra, "engineering_head")
        factory = FakeFactory({
            "backend_lead": fail_result("backend_lead", "database_layer_worker failed"),
            "frontend_lead": ok_result("frontend_lead"),
            "integration_lead": ok_result("integration_lead"),
        })
        eng_task.context.approved_artifacts["__factory__"] = factory
        result = await head.execute(eng_task)
        assert result.status == TaskStatus.ESCALATED

    @pytest.mark.asyncio
    async def test_pipeline_tolerates_frontend_skip(self, eng_task_no_ui):
        from services.engineering.head import EngineeringHead
        infra = make_infra()
        head = inject(EngineeringHead, infra, "engineering_head")
        factory = FakeFactory({
            "backend_lead": ok_result("backend_lead"),
            "frontend_lead": ok_result("frontend_lead", team="frontend", modules=0, skipped=True,
                                        reason="No ui_blueprint"),
            "integration_lead": ok_result("integration_lead"),
            "code_review_lead": ok_result("code_review_lead", reviewed=7, pull_request_id="pr-7"),
        })
        eng_task_no_ui.context.approved_artifacts["__factory__"] = factory
        result = await head.execute(eng_task_no_ui)
        assert result.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_pipeline_escalates_on_review_lead_failure(self, eng_task):
        from services.engineering.head import EngineeringHead
        infra = make_infra()
        head = inject(EngineeringHead, infra, "engineering_head")
        factory = FakeFactory({
            "backend_lead": ok_result("backend_lead"),
            "frontend_lead": ok_result("frontend_lead"),
            "integration_lead": ok_result("integration_lead"),
            "code_review_lead": fail_result("code_review_lead", "all modules blocked"),
        })
        eng_task.context.approved_artifacts["__factory__"] = factory
        result = await head.execute(eng_task)
        assert result.status == TaskStatus.ESCALATED

    @pytest.mark.asyncio
    async def test_pipeline_publishes_websocket_completion_event(self, eng_task):
        from services.engineering.head import EngineeringHead
        infra = make_infra()
        head = inject(EngineeringHead, infra, "engineering_head")
        eng_task.context.approved_artifacts["__factory__"] = self._full_success_factory()
        result = await head.execute(eng_task)
        assert any(e.event_type == "phase_completed" for e in result.ws_events)

    @pytest.mark.asyncio
    async def test_pipeline_stores_implementation_plan_in_context(self, eng_task):
        from services.engineering.head import EngineeringHead
        infra = make_infra()
        head = inject(EngineeringHead, infra, "engineering_head")
        eng_task.context.approved_artifacts["__factory__"] = self._full_success_factory()
        await head.execute(eng_task)
        assert "__implementation_plan__" in eng_task.context.approved_artifacts

    @pytest.mark.asyncio
    async def test_pipeline_runs_teams_without_factory_using_placeholders(self, eng_task):
        # No __factory__ injected — head must still complete gracefully via placeholders.
        from services.engineering.head import EngineeringHead
        infra = make_infra()
        head = inject(EngineeringHead, infra, "engineering_head")
        result = await head.execute(eng_task)
        assert result.status == TaskStatus.COMPLETED
