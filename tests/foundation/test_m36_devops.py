"""
tests/foundation/test_m36_devops.py
========================================
M3.6 DevOps Service tests — 4 layers matching the M3.1-M3.5 pattern.

Layer 1 — Unit:        models, utils (semver/templates), task decomposition,
                       validate/rollback/release gate logic, routing
                       predicates, agent registry verification, provider
                       interface
Layer 2 — Graph:       LangGraph node functions + graph construction
                       (including the approval interrupt)
Layer 3 — Integration: workers (deterministic — no LLM), read+release-write
                       Repository Service client (mocked httpx), deployment
                       ORM repository (interface-level), leads (fake factory)
Layer 4 — E2E:         full DevOpsHead pipeline, both stages (fake factory
                       chain) — plan generation, successful deploy+release,
                       and health-failure -> rollback
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

from services.devops.models import (
    ComposeArtifact,
    DeploymentPlan,
    DeploymentReport,
    DeploymentStatus,
    DevOpsPlan,
    DevOpsTask,
    DevOpsTaskStatus,
    DockerfileArtifact,
    EnvironmentConfigArtifact,
    HealthCheckResult,
    HealthReport,
    PipelineConfigArtifact,
    REQUIRED_HEALTH_CHECKS,
    Release,
    RollbackReport,
    RollbackStatus,
    ROLLBACK_TRIGGERS,
    VersionBump,
    WorkerTeam,
)
from services.devops.utils import (
    bump_version,
    exponential_backoff_seconds,
    idempotency_key,
    parse_llm_json,
    parse_semver,
    render_compose,
    render_dockerfile,
    render_env_example,
    render_github_actions,
)
from services.devops.context import (
    CICD_WORKERS,
    DEPLOYMENT_WORKERS,
    INFRASTRUCTURE_WORKERS,
    build_deployment_plan,
    build_deployment_report,
    build_devops_plan,
    build_health_report,
    build_release,
    build_rollback_report,
    decide_rollback,
    team_progress,
    topological_batches,
    validate_qa_and_security,
)
from services.devops.routing import (
    MAX_RETRY_CYCLES,
    MAX_TASK_RETRIES,
    route_after_approval_gate,
    route_after_deploy,
    route_after_generate_cicd,
    route_after_generate_infrastructure,
    route_after_health_check,
    route_after_rollback,
    route_after_validate_qa_security,
    route_checkpoint_recovery,
    route_task_retry,
)
from services.devops.providers import DeploymentProvider, DockerComposeProvider, KubernetesProvider, default_provider
from services.devops.integration.repository_client import (
    DevOpsRepositoryClient,
    RepositoryServiceClientError,
)
from services.devops.schemas import DevOpsServiceError


# ══════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════

@pytest.fixture
def project_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def devops_context(project_id) -> AgentContext:
    return AgentContext(
        project_id=project_id, workflow_id=str(uuid.uuid4()),
        current_phase=8, project_name="TestApp",
        project_description="A test SaaS app.",
        approved_artifacts={
            "source_code": {"files": [
                {"path": "app/main.py", "language": "python", "content": "x = 1"},
                {"path": "requirements.txt", "language": "text", "content": "flask==2.0.0\n"},
            ]},
            "qa_report": {"verdict": "pass", "tests_total": 183, "tests_passed": 183,
                          "blocking_conditions": []},
            "security_report": {"verdict": "pass", "risk_level": "low", "risk_score": 0.0,
                                "blocking_conditions": []},
            "openapi_spec": {"paths": {"/health": {}}},
            "database_schema": {"tables": [{"name": "users"}]},
        },
        tech_stack={"backend": "Python+FastAPI", "frontend": "React+TS"},
        llm_provider="anthropic", llm_model="claude-sonnet-4-6",
        budget_limit_usd=50.0, total_spend_usd=2.0,
    )


@pytest.fixture
def devops_task(project_id, devops_context) -> TaskInput:
    return TaskInput(
        task_id=str(uuid.uuid4()), project_id=project_id,
        agent_id="devops_head", parent_agent_id="manager_agent",
        task_type="run_devops_pipeline",
        description="Generate deployment plan for review",
        expected_output="DeploymentPlan artifact awaiting approval",
        context=devops_context,
    )


@pytest.fixture
def execute_task(project_id, devops_context) -> TaskInput:
    devops_context.approved_artifacts["deployment_plan"] = DeploymentPlan(
        project_id=project_id, proposed_version="0.2.0", qa_verdict="pass",
        security_verdict="pass", dockerfile_ref="a1", compose_ref="a2",
        pipeline_ref="a3", environment_ref="a4",
    ).model_dump()
    return TaskInput(
        task_id=str(uuid.uuid4()), project_id=project_id,
        agent_id="devops_head", parent_agent_id="manager_agent",
        task_type="execute_deployment",
        description="Execute approved deployment plan",
        expected_output="DeploymentReport",
        context=devops_context,
    )


def make_infra():
    inner_db = MagicMock(
        execute=AsyncMock(return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=None),
            scalar_one=MagicMock(return_value=0))),
        flush=AsyncMock(), add=MagicMock())
    db = MagicMock()
    db.__aenter__ = AsyncMock(return_value=inner_db)
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
    a.department = "devops"
    a.layer = layer
    a.role = role
    a.responsibilities = ["DevOps"]
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
                        content=content or {"generated": 1},
                        quality_score=0.9,
                        artifacts=[{"artifact_id": str(uuid.uuid4()),
                                    "artifact_type": content.get("__artifact_type__", "dockerfile"),
                                    "version": 1}])


def fail_result(agent_id: str, reason: str) -> AgentResult:
    return AgentResult(task_id="t", agent_id=agent_id, status=TaskStatus.FAILED,
                        content={}, quality_score=0.0, failure_reason=reason)


class FakeProvider(DeploymentProvider):
    name = "fake"

    def __init__(self, deploy_success=True, health_all_pass=True, rollback_success=True):
        self._deploy_success = deploy_success
        self._health_all_pass = health_all_pass
        self._rollback_success = rollback_success

    async def deploy(self, project_id, plan):
        return {"success": self._deploy_success, "deployment_ref": f"fake::{project_id}",
                "detail": "ok" if self._deploy_success else "deploy failed"}

    async def health_check(self, project_id, deployment_ref):
        checks = [{"check_name": n, "passed": self._health_all_pass,
                   "detail": "ok" if self._health_all_pass else "fail"} for n in REQUIRED_HEALTH_CHECKS]
        return {"checks": checks}

    async def rollback(self, project_id, deployment_ref, reason):
        return {"success": self._rollback_success, "detail": "rolled back" if self._rollback_success else "failed"}


# ══════════════════════════════════════════════════════════════
# LAYER 1a — Unit: models
# ══════════════════════════════════════════════════════════════

class TestArtifactModels:
    def test_dockerfile_artifact_defaults(self):
        d = DockerfileArtifact(project_id="p", content="FROM python")
        assert d.base_image == "python:3.12-slim" and d.exposed_port == 8000

    def test_compose_artifact_holds_services(self):
        c = ComposeArtifact(project_id="p", content="version: 3", services=["app", "postgres"])
        assert len(c.services) == 2

    def test_pipeline_config_default_workflow_name(self):
        p = PipelineConfigArtifact(project_id="p", content="name: CI")
        assert p.workflow_name == "build.yml"

    def test_environment_config_variables(self):
        e = EnvironmentConfigArtifact(project_id="p", content="A=1", variables={"A": "1"})
        assert e.variables["A"] == "1"


class TestDeploymentPlan:
    def test_ready_for_approval_true_without_blocking(self):
        p = DeploymentPlan(project_id="p")
        assert p.ready_for_approval is True

    def test_ready_for_approval_false_with_blocking(self):
        p = DeploymentPlan(project_id="p", blocking_reasons=["QA failed"])
        assert p.ready_for_approval is False

    def test_default_environment_is_production(self):
        p = DeploymentPlan(project_id="p")
        assert p.target_environment == "production"


class TestHealthReport:
    def test_all_passed_true_when_all_checks_pass(self):
        r = HealthReport(project_id="p", deployment_id="d", checks=[
            HealthCheckResult(check_name=n, passed=True) for n in REQUIRED_HEALTH_CHECKS])
        assert r.all_passed is True
        assert r.failed_checks == []

    def test_all_passed_false_when_empty(self):
        r = HealthReport(project_id="p", deployment_id="d")
        assert r.all_passed is False

    def test_all_passed_false_with_one_failure(self):
        checks = [HealthCheckResult(check_name=n, passed=True) for n in REQUIRED_HEALTH_CHECKS]
        checks[0] = HealthCheckResult(check_name=checks[0].check_name, passed=False)
        r = HealthReport(project_id="p", deployment_id="d", checks=checks)
        assert r.all_passed is False
        assert len(r.failed_checks) == 1

    def test_required_health_checks_count(self):
        assert len(REQUIRED_HEALTH_CHECKS) == 6


class TestRollbackReport:
    def test_default_status_initiated(self):
        r = RollbackReport(project_id="p", deployment_id="d", reason="x")
        assert r.status == RollbackStatus.INITIATED

    def test_rollback_triggers_count(self):
        assert len(ROLLBACK_TRIGGERS) == 6
        assert "deployment_failure" in ROLLBACK_TRIGGERS
        assert "health_failure" in ROLLBACK_TRIGGERS


class TestRelease:
    def test_release_defaults(self):
        r = Release(project_id="p", version="0.1.0")
        assert r.previous_version is None
        assert r.release_notes == ""


class TestDeploymentReport:
    def test_succeeded_true_when_healthy(self):
        r = DeploymentReport(project_id="p", deployment_id="d", status=DeploymentStatus.HEALTHY)
        assert r.succeeded is True

    def test_succeeded_false_when_failed(self):
        r = DeploymentReport(project_id="p", deployment_id="d", status=DeploymentStatus.FAILED)
        assert r.succeeded is False

    def test_succeeded_false_when_rolled_back(self):
        r = DeploymentReport(project_id="p", deployment_id="d", status=DeploymentStatus.ROLLED_BACK)
        assert r.succeeded is False


class TestDevOpsTask:
    def test_can_run_with_no_dependencies(self):
        t = DevOpsTask(project_id="p", team=WorkerTeam.INFRASTRUCTURE, worker_agent_id="w")
        assert t.can_run(set()) is True

    def test_can_run_false_when_dependency_incomplete(self):
        t = DevOpsTask(project_id="p", team=WorkerTeam.DEPLOYMENT, worker_agent_id="w", depends_on=["dep-1"])
        assert t.can_run(set()) is False

    def test_can_run_true_when_dependency_complete(self):
        t = DevOpsTask(project_id="p", team=WorkerTeam.DEPLOYMENT, worker_agent_id="w", depends_on=["dep-1"])
        assert t.can_run({"dep-1"}) is True

    def test_can_run_false_when_not_pending(self):
        t = DevOpsTask(project_id="p", team=WorkerTeam.INFRASTRUCTURE, worker_agent_id="w",
                        status=DevOpsTaskStatus.RUNNING)
        assert t.can_run(set()) is False

    @pytest.mark.parametrize("retries,expected", [(0, 1), (1, 2), (2, 4), (3, 8), (10, 60)])
    def test_backoff_seconds_exponential_capped(self, retries, expected):
        t = DevOpsTask(project_id="p", team=WorkerTeam.INFRASTRUCTURE, worker_agent_id="w", retry_count=retries)
        assert t.next_backoff_seconds() == expected


class TestDevOpsPlan:
    def _plan(self):
        t1 = DevOpsTask(project_id="p", team=WorkerTeam.INFRASTRUCTURE, worker_agent_id="dockerfile_writer_worker")
        t2 = DevOpsTask(project_id="p", team=WorkerTeam.DEPLOYMENT, worker_agent_id="health_check_worker",
                         depends_on=[t1.task_id])
        t3 = DevOpsTask(project_id="p", team=WorkerTeam.CICD, worker_agent_id="pipeline_config_worker",
                         status=DevOpsTaskStatus.COMPLETED)
        return DevOpsPlan(project_id="p", feature_name="f", tasks=[t1, t2, t3]), t1, t2, t3

    def test_ready_tasks_returns_only_unblocked(self):
        plan, t1, t2, t3 = self._plan()
        ready = plan.ready_tasks(set())
        assert t1 in ready and t2 not in ready

    def test_tasks_by_team_filters_correctly(self):
        plan, t1, t2, t3 = self._plan()
        assert plan.tasks_by_team(WorkerTeam.INFRASTRUCTURE) == [t1]

    def test_all_complete_false_when_pending_remain(self):
        plan, *_ = self._plan()
        assert plan.all_complete is False

    def test_all_complete_true_when_all_completed(self):
        plan, t1, t2, t3 = self._plan()
        t1.status = DevOpsTaskStatus.COMPLETED
        t2.status = DevOpsTaskStatus.COMPLETED
        assert plan.all_complete is True

    def test_any_dead_lettered_true_when_flagged(self):
        plan, t1, t2, t3 = self._plan()
        t2.dead_lettered = True
        assert plan.any_dead_lettered is True


# ══════════════════════════════════════════════════════════════
# LAYER 1b — Unit: utils
# ══════════════════════════════════════════════════════════════

class TestParseLlmJson:
    def test_parses_plain_json(self):
        assert parse_llm_json('{"a": 1}') == {"a": 1}

    def test_strips_markdown_fences(self):
        assert parse_llm_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_returns_fallback_on_invalid_json(self):
        assert parse_llm_json("not json", {"x": 1}) == {"x": 1}


class TestIdempotencyKey:
    def test_deterministic(self):
        assert idempotency_key("p", "t", "w") == idempotency_key("p", "t", "w")

    def test_differs_by_worker(self):
        assert idempotency_key("p", "t", "w1") != idempotency_key("p", "t", "w2")


class TestExponentialBackoff:
    @pytest.mark.parametrize("retries,expected", [(0, 1), (1, 2), (2, 4), (6, 60)])
    def test_backoff_values(self, retries, expected):
        assert exponential_backoff_seconds(retries) == expected


class TestSemver:
    def test_parse_semver_valid(self):
        assert parse_semver("1.4.2") == (1, 4, 2)

    def test_parse_semver_invalid_defaults(self):
        assert parse_semver("not-a-version") == (0, 1, 0)

    def test_bump_version_none_previous_gives_0_1_0(self):
        assert bump_version(None) == "0.1.0"

    def test_bump_version_patch(self):
        assert bump_version("1.4.2", VersionBump.PATCH) == "1.4.3"

    def test_bump_version_minor(self):
        assert bump_version("1.4.2", VersionBump.MINOR) == "1.5.0"

    def test_bump_version_major(self):
        assert bump_version("1.4.2", VersionBump.MAJOR) == "2.0.0"

    def test_bump_version_default_is_patch(self):
        assert bump_version("1.0.0") == "1.0.1"


class TestRenderDockerfile:
    def test_python_stack_produces_uvicorn_cmd(self):
        content = render_dockerfile({"backend": "Python+FastAPI"}, exposed_port=8000)
        assert "uvicorn" in content and "EXPOSE 8000" in content

    def test_node_stack_produces_node_cmd(self):
        content = render_dockerfile({"backend": "Node+Express"}, exposed_port=3000)
        assert "node" in content and "EXPOSE 3000" in content

    def test_empty_stack_defaults_to_python(self):
        content = render_dockerfile({}, exposed_port=8000)
        assert "uvicorn" in content


class TestRenderCompose:
    def test_includes_postgres_and_nats(self):
        content, services = render_compose("MyApp", exposed_port=8000)
        assert "postgres" in services and "nats" in services
        assert "postgres" in content and "nats" in content

    def test_port_mapping_present(self):
        content, _ = render_compose("MyApp", exposed_port=9090)
        assert "9090:9090" in content


class TestRenderGithubActions:
    def test_contains_checkout_step(self):
        content = render_github_actions("MyApp")
        assert "actions/checkout" in content


class TestRenderEnvExample:
    def test_includes_database_url(self):
        content, variables = render_env_example({}, {})
        assert "DATABASE_URL" in variables
        assert "DATABASE_URL=" in content

    def test_adds_pool_size_when_tables_present(self):
        _, variables = render_env_example({}, {"tables": [{"name": "users"}]})
        assert "DB_POOL_SIZE" in variables

    def test_no_pool_size_without_tables(self):
        _, variables = render_env_example({}, {})
        assert "DB_POOL_SIZE" not in variables


# ══════════════════════════════════════════════════════════════
# LAYER 1c — Unit: task decomposition + gate logic
# ══════════════════════════════════════════════════════════════

class TestBuildDevOpsPlan:
    def test_includes_all_three_teams(self, project_id):
        plan = build_devops_plan(project_id, "f", {})
        teams = {t.team for t in plan.tasks}
        assert teams == {WorkerTeam.INFRASTRUCTURE, WorkerTeam.CICD, WorkerTeam.DEPLOYMENT}

    def test_worker_count_matches_registry(self, project_id):
        plan = build_devops_plan(project_id, "f", {})
        expected = len(INFRASTRUCTURE_WORKERS) + len(CICD_WORKERS) + len(DEPLOYMENT_WORKERS)
        assert len(plan.tasks) == expected

    def test_health_check_depends_on_provisioner(self, project_id):
        plan = build_devops_plan(project_id, "f", {})
        health_task = next(t for t in plan.tasks if t.worker_agent_id == "health_check_worker")
        provisioner_task = next(t for t in plan.tasks if t.worker_agent_id == "provisioner_worker")
        assert provisioner_task.task_id in health_task.depends_on

    def test_plan_stores_upstream_refs(self, project_id):
        refs = {"source_code": {}}
        plan = build_devops_plan(project_id, "f", refs)
        assert plan.upstream_refs == refs


class TestTopologicalBatches:
    def test_independent_tasks_one_batch(self):
        tasks = [DevOpsTask(project_id="p", team=WorkerTeam.CICD, worker_agent_id=f"w{i}") for i in range(2)]
        batches = topological_batches(tasks)
        assert len(batches) == 1

    def test_dependent_tasks_split(self):
        t1 = DevOpsTask(project_id="p", team=WorkerTeam.DEPLOYMENT, worker_agent_id="provisioner_worker")
        t2 = DevOpsTask(project_id="p", team=WorkerTeam.DEPLOYMENT, worker_agent_id="health_check_worker",
                         depends_on=[t1.task_id])
        batches = topological_batches([t1, t2])
        assert len(batches) == 2

    def test_cycle_raises(self):
        t1 = DevOpsTask(project_id="p", team=WorkerTeam.CICD, worker_agent_id="w1")
        t2 = DevOpsTask(project_id="p", team=WorkerTeam.CICD, worker_agent_id="w2", depends_on=[t1.task_id])
        t1.depends_on = [t2.task_id]
        with pytest.raises(ValueError):
            topological_batches([t1, t2])

    def test_real_deployment_batches(self, project_id):
        plan = build_devops_plan(project_id, "f", {})
        deploy_tasks = plan.tasks_by_team(WorkerTeam.DEPLOYMENT)
        batches = topological_batches(deploy_tasks)
        assert len(batches) == 2  # provisioner, then health_check


class TestTeamProgress:
    def test_progress_counts(self, project_id):
        plan = build_devops_plan(project_id, "f", {})
        plan.tasks[0].status = DevOpsTaskStatus.COMPLETED
        progress = team_progress(plan, plan.tasks[0].team)
        assert progress["completed"] >= 1


class TestValidateQaAndSecurity:
    def test_no_blocking_when_both_pass(self):
        assert validate_qa_and_security({"verdict": "pass"}, {"verdict": "pass"}) == []

    def test_no_blocking_when_both_warn(self):
        assert validate_qa_and_security({"verdict": "warn"}, {"verdict": "warn"}) == []

    def test_blocking_when_qa_missing(self):
        reasons = validate_qa_and_security(None, {"verdict": "pass"})
        assert any("QA" in r for r in reasons)

    def test_blocking_when_security_missing(self):
        reasons = validate_qa_and_security({"verdict": "pass"}, None)
        assert any("Security" in r for r in reasons)

    def test_blocking_when_qa_failed(self):
        reasons = validate_qa_and_security({"verdict": "fail", "blocking_conditions": ["x"]}, {"verdict": "pass"})
        assert any("QA gate failed" in r for r in reasons)

    def test_blocking_when_security_failed(self):
        reasons = validate_qa_and_security({"verdict": "pass"}, {"verdict": "fail", "blocking_conditions": ["y"]})
        assert any("Security gate failed" in r for r in reasons)

    def test_both_failed_gives_two_reasons(self):
        reasons = validate_qa_and_security({"verdict": "fail"}, {"verdict": "fail"})
        assert len(reasons) == 2


class TestBuildDeploymentPlan:
    def test_plan_carries_verdicts_and_refs(self, project_id):
        plan = build_deployment_plan(
            project_id, {"verdict": "pass"}, {"verdict": "warn", "risk_level": "medium"},
            "0.2.0", {"dockerfile": "a1", "docker_compose": "a2"}, [])
        assert plan.qa_verdict == "pass"
        assert plan.security_verdict == "warn"
        assert plan.risk_level == "medium"
        assert plan.dockerfile_ref == "a1"

    def test_plan_with_blocking_reasons_not_ready(self, project_id):
        plan = build_deployment_plan(project_id, None, None, "0.1.0", {}, ["missing QA"])
        assert plan.ready_for_approval is False


class TestDecideRollback:
    def test_no_rollback_when_deploy_succeeded_and_healthy(self, project_id):
        hr = HealthReport(project_id=project_id, deployment_id="d",
                           checks=[HealthCheckResult(check_name=n, passed=True) for n in REQUIRED_HEALTH_CHECKS])
        assert decide_rollback(hr, True) is None

    def test_rollback_on_deploy_failure(self, project_id):
        hr = HealthReport(project_id=project_id, deployment_id="d")
        reason = decide_rollback(hr, False)
        assert reason is not None and "deployment_failure" in reason

    def test_rollback_on_health_failure(self, project_id):
        checks = [HealthCheckResult(check_name=n, passed=True) for n in REQUIRED_HEALTH_CHECKS]
        checks[0] = HealthCheckResult(check_name=checks[0].check_name, passed=False)
        hr = HealthReport(project_id=project_id, deployment_id="d", checks=checks)
        reason = decide_rollback(hr, True)
        assert reason is not None and "health_failure" in reason


class TestBuildHealthReport:
    def test_builds_from_raw_checks(self, project_id):
        raw = [{"check_name": "service_reachable", "passed": True, "detail": "ok"}]
        hr = build_health_report(project_id, "d", raw)
        assert len(hr.checks) == 1


class TestBuildRollbackReport:
    def test_status_completed_when_succeeded(self, project_id):
        rr = build_rollback_report(project_id, "d", "reason", "1.0.0", True)
        assert rr.status == RollbackStatus.COMPLETED

    def test_status_failed_when_not_succeeded(self, project_id):
        rr = build_rollback_report(project_id, "d", "reason", None, False)
        assert rr.status == RollbackStatus.FAILED


class TestBuildRelease:
    def test_includes_version_in_notes(self, project_id):
        rel = build_release(project_id, "1.2.0", "1.1.0", {"verdict": "pass"}, {"verdict": "pass"})
        assert "1.2.0" in rel.release_notes
        assert "1.1.0" in rel.release_notes

    def test_handles_missing_reports(self, project_id):
        rel = build_release(project_id, "0.1.0", None, None, None)
        assert rel.version == "0.1.0"


class TestBuildDeploymentReport:
    def test_healthy_report_succeeded(self, project_id):
        report = build_deployment_report(project_id, "d", DeploymentStatus.HEALTHY, "1.0.0", None, None, None, [])
        assert report.succeeded is True


# ══════════════════════════════════════════════════════════════
# LAYER 1d — Unit: routing predicates
# ══════════════════════════════════════════════════════════════

class TestDevOpsRouting:
    def test_route_after_validate_ok(self):
        assert route_after_validate_qa_security({"phase_status": "running"}) == "generate_infrastructure"

    def test_route_after_validate_failed(self):
        assert route_after_validate_qa_security({"phase_status": "failed"}) == "failed"

    def test_route_after_infra_ok(self):
        assert route_after_generate_infrastructure({"phase_status": "running"}) == "generate_cicd"

    def test_route_after_infra_dlq(self):
        assert route_after_generate_infrastructure({"phase_status": "running", "any_dead_lettered": True}) == "dlq"

    def test_route_after_cicd_ok(self):
        assert route_after_generate_cicd({"phase_status": "running"}) == "approval_gate"

    def test_route_after_approval_gate_approved(self):
        assert route_after_approval_gate({"approval_status": "approved"}) == "deploy"

    def test_route_after_approval_gate_rejected(self):
        assert route_after_approval_gate({"approval_status": "rejected"}) == "failed"

    def test_route_after_approval_gate_pending(self):
        assert route_after_approval_gate({}) == "awaiting_approval"

    def test_route_after_deploy_ok(self):
        assert route_after_deploy({"phase_status": "running"}) == "health_check"

    def test_route_after_deploy_failed(self):
        assert route_after_deploy({"phase_status": "failed"}) == "failed"

    def test_route_after_health_check_pass(self):
        assert route_after_health_check({"health_passed": True}) == "release"

    def test_route_after_health_check_fail(self):
        assert route_after_health_check({"health_passed": False}) == "rollback"

    def test_route_after_rollback_within_budget(self):
        assert route_after_rollback({"retry_cycles_run": 1}) == "notify_manager_failed"

    def test_route_after_rollback_exhausted(self):
        assert route_after_rollback({"retry_cycles_run": MAX_RETRY_CYCLES}) == "failed"

    def test_route_task_retry_done(self):
        assert route_task_retry({"status": "completed"}) == "done"

    def test_route_task_retry_retry(self):
        assert route_task_retry({"status": "failed", "retry_count": 1}) == "retry"

    def test_route_task_retry_dead_letter(self):
        assert route_task_retry({"status": "failed", "retry_count": MAX_TASK_RETRIES}) == "dead_letter"

    def test_route_checkpoint_recovery_valid(self):
        assert route_checkpoint_recovery({"resume_at_stage": "deploy"}) == "deploy"

    def test_route_checkpoint_recovery_invalid_defaults(self):
        assert route_checkpoint_recovery({"resume_at_stage": "bogus"}) == "validate"


# ══════════════════════════════════════════════════════════════
# LAYER 1e — Unit: agent registry
# ══════════════════════════════════════════════════════════════

class TestDevOpsRegistry:
    DEVOPS_AGENT_IDS = [
        "devops_head", "container_lead", "dockerfile_writer_worker", "docker_compose_worker",
        "cicd_lead", "pipeline_config_worker", "environment_config_worker",
        "infrastructure_ops_lead", "provisioner_worker", "health_check_worker",
    ]

    def test_all_devops_agent_ids_registered(self):
        for agent_id in self.DEVOPS_AGENT_IDS:
            assert agent_id in AGENT_REGISTRY, f"{agent_id} missing from AGENT_REGISTRY"

    def test_devops_department_has_exactly_ten_agents(self):
        devops_agents = [a for a in AGENT_REGISTRY.values() if a.department == "devops"]
        assert len(devops_agents) == 10

    def test_devops_head_layer_and_parent(self):
        spec = AGENT_REGISTRY["devops_head"]
        assert spec.layer == 3 and spec.role == "head" and spec.parent_agent_id == "manager_agent"

    @pytest.mark.parametrize("lead_id", ["container_lead", "cicd_lead", "infrastructure_ops_lead"])
    def test_leads_report_to_devops_head(self, lead_id):
        spec = AGENT_REGISTRY[lead_id]
        assert spec.layer == 4 and spec.role == "lead" and spec.parent_agent_id == "devops_head"

    def test_dockerfile_writer_reports_to_container_lead(self):
        assert AGENT_REGISTRY["dockerfile_writer_worker"].parent_agent_id == "container_lead"

    def test_docker_compose_worker_reports_to_container_lead(self):
        assert AGENT_REGISTRY["docker_compose_worker"].parent_agent_id == "container_lead"

    def test_pipeline_config_worker_reports_to_cicd_lead(self):
        assert AGENT_REGISTRY["pipeline_config_worker"].parent_agent_id == "cicd_lead"

    def test_environment_config_worker_reports_to_cicd_lead(self):
        assert AGENT_REGISTRY["environment_config_worker"].parent_agent_id == "cicd_lead"

    def test_provisioner_worker_reports_to_infrastructure_ops_lead(self):
        assert AGENT_REGISTRY["provisioner_worker"].parent_agent_id == "infrastructure_ops_lead"

    def test_health_check_worker_reports_to_infrastructure_ops_lead(self):
        assert AGENT_REGISTRY["health_check_worker"].parent_agent_id == "infrastructure_ops_lead"

    def test_factory_creates_devops_head(self):
        from core.runtime.factory import AgentFactory
        import services.devops  # noqa: F401
        factory = AgentFactory(db_factory=lambda: None, nats=None, storage=None,
                                audit_repo=None, artifact_repo=None, token_repo=None)
        agent = factory.create("devops_head")
        assert agent.agent_id == "devops_head" and agent.department == "devops"

    def test_factory_creates_every_devops_worker(self):
        from core.runtime.factory import AgentFactory
        import services.devops  # noqa: F401
        factory = AgentFactory(db_factory=lambda: None, nats=None, storage=None,
                                audit_repo=None, artifact_repo=None, token_repo=None)
        for agent_id in self.DEVOPS_AGENT_IDS:
            agent = factory.create(agent_id)
            assert agent.agent_id == agent_id


# ══════════════════════════════════════════════════════════════
# LAYER 1f — Unit: providers
# ══════════════════════════════════════════════════════════════

class TestDeploymentProviders:
    def test_default_provider_is_docker_compose(self):
        assert isinstance(default_provider(), DockerComposeProvider)

    @pytest.mark.asyncio
    async def test_docker_compose_deploy_success(self, project_id):
        provider = DockerComposeProvider()
        result = await provider.deploy(project_id, {"compose_ref": "a1"})
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_docker_compose_deploy_fails_without_compose_ref(self, project_id):
        provider = DockerComposeProvider()
        result = await provider.deploy(project_id, {})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_docker_compose_deploy_forced_failure(self, project_id):
        provider = DockerComposeProvider(force_deploy_failure=True)
        result = await provider.deploy(project_id, {"compose_ref": "a1"})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_docker_compose_health_check_all_pass_by_default(self, project_id):
        provider = DockerComposeProvider()
        result = await provider.health_check(project_id, "ref")
        assert all(c["passed"] for c in result["checks"])
        assert len(result["checks"]) == len(REQUIRED_HEALTH_CHECKS)

    @pytest.mark.asyncio
    async def test_docker_compose_health_check_override(self, project_id):
        provider = DockerComposeProvider(health_override={"database_connected": False})
        result = await provider.health_check(project_id, "ref")
        db_check = next(c for c in result["checks"] if c["check_name"] == "database_connected")
        assert db_check["passed"] is False

    @pytest.mark.asyncio
    async def test_docker_compose_rollback_success(self, project_id):
        provider = DockerComposeProvider()
        result = await provider.rollback(project_id, "ref", "reason")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_docker_compose_rollback_forced_failure(self, project_id):
        provider = DockerComposeProvider(force_rollback_failure=True)
        result = await provider.rollback(project_id, "ref", "reason")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_kubernetes_provider_deploy_not_implemented(self, project_id):
        provider = KubernetesProvider()
        with pytest.raises(NotImplementedError):
            await provider.deploy(project_id, {})

    @pytest.mark.asyncio
    async def test_kubernetes_provider_health_check_not_implemented(self, project_id):
        provider = KubernetesProvider()
        with pytest.raises(NotImplementedError):
            await provider.health_check(project_id, "ref")

    @pytest.mark.asyncio
    async def test_kubernetes_provider_rollback_not_implemented(self, project_id):
        provider = KubernetesProvider()
        with pytest.raises(NotImplementedError):
            await provider.rollback(project_id, "ref", "reason")


# ══════════════════════════════════════════════════════════════
# LAYER 2 — Graph: node functions + graph construction
# ══════════════════════════════════════════════════════════════

class TestDevOpsGraph:
    def _base_state(self, project_id="p") -> Dict[str, Any]:
        return {
            "project_id": project_id, "workflow_id": "wf-1", "feature_name": "f",
            "qa_passed": True, "security_passed": True, "infra_ready": False, "cicd_ready": False,
            "approval_status": None, "deploy_succeeded": False, "health_passed": False,
            "retry_cycles_run": 0, "any_dead_lettered": False, "dlq_tasks": [],
            "version": "0.1.0", "phase_status": "running", "failure_reason": None,
            "resume_at_stage": None, "nats_events_queue": [], "ws_events_queue": [],
        }

    def test_graph_builds_without_error(self):
        from services.devops.workflows.devops_graph import build_devops_graph
        assert build_devops_graph() is not None

    def test_graph_builds_with_checkpointer_kwarg(self):
        from services.devops.workflows.devops_graph import build_devops_graph
        assert build_devops_graph(checkpointer=None) is not None

    @pytest.mark.asyncio
    async def test_receive_manager_approval_node(self):
        from services.devops.workflows.devops_graph import receive_manager_approval_node
        r = await receive_manager_approval_node(self._base_state())
        assert r["phase_status"] == "running"

    @pytest.mark.asyncio
    async def test_validate_qa_security_node_valid(self):
        from services.devops.workflows.devops_graph import validate_qa_security_node
        r = await validate_qa_security_node(self._base_state())
        assert r["phase_status"] == "running"

    @pytest.mark.asyncio
    async def test_validate_qa_security_node_invalid(self):
        from services.devops.workflows.devops_graph import validate_qa_security_node
        s = self._base_state(); s["qa_passed"] = False
        r = await validate_qa_security_node(s)
        assert r["phase_status"] == "failed"

    @pytest.mark.asyncio
    async def test_generate_infrastructure_node(self):
        from services.devops.workflows.devops_graph import generate_infrastructure_node
        r = await generate_infrastructure_node(self._base_state())
        assert r["infra_ready"] is True

    @pytest.mark.asyncio
    async def test_generate_cicd_node(self):
        from services.devops.workflows.devops_graph import generate_cicd_node
        r = await generate_cicd_node(self._base_state())
        assert r["cicd_ready"] is True

    @pytest.mark.asyncio
    async def test_approval_gate_node_sets_awaiting_approval(self):
        from services.devops.workflows.devops_graph import approval_gate_node
        r = await approval_gate_node(self._base_state())
        assert r["phase_status"] == "awaiting_approval"

    @pytest.mark.asyncio
    async def test_deploy_node_publishes_deployment_started(self):
        from services.devops.workflows.devops_graph import deploy_node
        r = await deploy_node(self._base_state())
        subjects = [e["subject"] for e in r["nats_events_queue"]]
        assert "deployment.started" in subjects

    @pytest.mark.asyncio
    async def test_health_check_node_publishes_health_completed(self):
        from services.devops.workflows.devops_graph import health_check_node
        r = await health_check_node(self._base_state())
        subjects = [e["subject"] for e in r["nats_events_queue"]]
        assert "health.completed" in subjects

    @pytest.mark.asyncio
    async def test_release_node_completes_phase(self):
        from services.devops.workflows.devops_graph import release_node
        r = await release_node(self._base_state())
        assert r["phase_status"] == "completed"
        subjects = [e["subject"] for e in r["nats_events_queue"]]
        assert "deployment.completed" in subjects and "devops.phase.completed" in subjects

    @pytest.mark.asyncio
    async def test_rollback_node_increments_cycle(self):
        from services.devops.workflows.devops_graph import rollback_node
        s = self._base_state(); s["retry_cycles_run"] = 1
        r = await rollback_node(s)
        assert r["retry_cycles_run"] == 2
        subjects = [e["subject"] for e in r["nats_events_queue"]]
        assert "rollback.completed" in subjects

    @pytest.mark.asyncio
    async def test_notify_manager_failed_node(self):
        from services.devops.workflows.devops_graph import notify_manager_failed_node
        r = await notify_manager_failed_node(self._base_state())
        subjects = [e["subject"] for e in r["nats_events_queue"]]
        assert "deployment.failed" in subjects and "devops.phase.completed" in subjects

    @pytest.mark.asyncio
    async def test_dlq_node(self):
        from services.devops.workflows.devops_graph import dlq_node
        s = self._base_state(); s["dlq_tasks"] = ["t1"]
        r = await dlq_node(s)
        assert r["phase_status"] == "failed" and "t1" in r["failure_reason"]

    @pytest.mark.asyncio
    async def test_handle_failure_node(self):
        from services.devops.workflows.devops_graph import handle_failure_node
        s = self._base_state(); s["failure_reason"] = "boom"
        r = await handle_failure_node(s)
        subjects = [e["subject"] for e in r["nats_events_queue"]]
        assert "devops.phase.failed" in subjects


# ══════════════════════════════════════════════════════════════
# LAYER 3a — Integration: Repository Service client (read + release write)
# ══════════════════════════════════════════════════════════════

class TestDevOpsRepositoryClient:
    def _mock_client(self, json_body: Any, status: int = 200):
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
    async def test_get_repository(self):
        client = DevOpsRepositoryClient(base_url="http://test")
        mock_client = self._mock_client({"id": "repo-1"})
        with patch("services.devops.integration.repository_client.httpx.AsyncClient", return_value=mock_client):
            result = await client.get_repository("p1")
        mock_client.request.assert_called_once_with("GET", "/repositories/p1", json=None)
        assert result["id"] == "repo-1"

    @pytest.mark.asyncio
    async def test_list_branches(self):
        client = DevOpsRepositoryClient(base_url="http://test")
        mock_client = self._mock_client([{"name": "main"}])
        with patch("services.devops.integration.repository_client.httpx.AsyncClient", return_value=mock_client):
            result = await client.list_branches("p1")
        assert result[0]["name"] == "main"

    @pytest.mark.asyncio
    async def test_create_release_posts_correct_payload(self):
        client = DevOpsRepositoryClient(base_url="http://test")
        mock_client = self._mock_client({"tag_name": "v1.0.0"})
        with patch("services.devops.integration.repository_client.httpx.AsyncClient", return_value=mock_client):
            result = await client.create_release("p1", "v1.0.0", name="Release 1.0.0", body="notes")
        args, kwargs = mock_client.request.call_args
        assert args[0] == "POST" and args[1] == "/releases"
        assert kwargs["json"]["tag_name"] == "v1.0.0"
        assert result["tag_name"] == "v1.0.0"

    @pytest.mark.asyncio
    async def test_rollback_release_posts_correct_payload(self):
        client = DevOpsRepositoryClient(base_url="http://test")
        mock_client = self._mock_client({"status": "rolled_back"})
        with patch("services.devops.integration.repository_client.httpx.AsyncClient", return_value=mock_client):
            result = await client.rollback_release("p1", "v1.0.0", "health check failed")
        args, kwargs = mock_client.request.call_args
        assert args[0] == "POST" and args[1] == "/releases/rollback"
        assert kwargs["json"]["reason"] == "health check failed"

    @pytest.mark.asyncio
    async def test_error_response_raises(self):
        client = DevOpsRepositoryClient(base_url="http://test")
        mock_client = self._mock_client({"detail": "error"}, status=500)
        with patch("services.devops.integration.repository_client.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(RepositoryServiceClientError) as exc_info:
                await client.get_repository("p1")
        assert exc_info.value.status_code == 500

    def test_default_base_url_uses_settings_port(self):
        client = DevOpsRepositoryClient()
        assert "8006" in client._base_url

    def test_client_has_no_source_write_methods(self):
        """DevOps may create releases/tags but must never expose a way to
        commit, push, merge, or modify source code — Engineering-only."""
        forbidden = ("commit_files", "merge", "create_branch", "push")
        public_methods = [m for m in dir(DevOpsRepositoryClient) if not m.startswith("_")]
        for m in public_methods:
            assert not any(f in m for f in forbidden), f"DevOps client exposes source-write method: {m}"


# ══════════════════════════════════════════════════════════════
# LAYER 3b — Integration: deployment_repository (interface-level)
# ══════════════════════════════════════════════════════════════

class TestDeploymentRepositoryInterface:
    """
    These check the repository classes expose the right static methods
    with the right call shape against a fully-mocked AsyncSession — full
    behavioral verification requires a live Postgres instance (see
    docs/M3.6_DevOps_Service_Handover.md's testing note).
    """

    @pytest.mark.asyncio
    async def test_deployment_repository_create_adds_and_flushes(self):
        from services.devops.integration.deployment_repository import DeploymentRepository
        db = MagicMock(add=MagicMock(), flush=AsyncMock())
        d = await DeploymentRepository.create(db, "p1", version="0.1.0")
        db.add.assert_called_once()
        db.flush.assert_awaited_once()
        assert d.project_id == "p1"

    @pytest.mark.asyncio
    async def test_deployment_history_repository_record(self):
        from services.devops.integration.deployment_repository import DeploymentHistoryRepository
        db = MagicMock(add=MagicMock(), flush=AsyncMock())
        h = await DeploymentHistoryRepository.record(db, "d1", "p1", "deployment.attempted", "deploying")
        db.add.assert_called_once()
        assert h.event_type == "deployment.attempted"

    @pytest.mark.asyncio
    async def test_deployment_health_repository_record_checks(self):
        from services.devops.integration.deployment_repository import DeploymentHealthRepository
        db = MagicMock(add=MagicMock(), flush=AsyncMock())
        checks = [{"check_name": "service_reachable", "passed": True, "detail": "ok"}]
        rows = await DeploymentHealthRepository.record_checks(db, "d1", "p1", checks)
        assert len(rows) == 1
        assert db.add.call_count == 1

    @pytest.mark.asyncio
    async def test_release_metadata_repository_create(self):
        from services.devops.integration.deployment_repository import ReleaseMetadataRepository
        db = MagicMock(add=MagicMock(), flush=AsyncMock())
        rel = await ReleaseMetadataRepository.create(db, "p1", "1.0.0", deployment_id="d1")
        assert rel.version == "1.0.0"

    @pytest.mark.asyncio
    async def test_rollback_record_repository_create(self):
        from services.devops.integration.deployment_repository import RollbackRecordRepository
        db = MagicMock(add=MagicMock(), flush=AsyncMock())
        rb = await RollbackRecordRepository.create(db, "d1", "p1", "health failed")
        assert rb.status == "initiated"


# ══════════════════════════════════════════════════════════════
# LAYER 3c — Integration: DevOps workers (deterministic — no LLM)
# ══════════════════════════════════════════════════════════════

class TestDockerfileWriterWorker:
    @pytest.mark.asyncio
    async def test_generates_python_dockerfile(self, devops_task):
        from services.devops.workers.dockerfile import DockerfileWriterWorker
        infra = make_infra()
        agent = inject(DockerfileWriterWorker, infra, "dockerfile_writer_worker")
        with patch.object(agent, "_pre_execute", AsyncMock()), patch.object(agent, "_post_execute", AsyncMock()):
            result = await agent.execute(devops_task)
        assert result.status == TaskStatus.COMPLETED
        assert "uvicorn" in result.content["content"]

    @pytest.mark.asyncio
    async def test_respects_port_override(self, devops_task):
        from services.devops.workers.dockerfile import DockerfileWriterWorker
        infra = make_infra()
        agent = inject(DockerfileWriterWorker, infra, "dockerfile_writer_worker")
        devops_task.context.approved_artifacts["__exposed_port_override__"] = 9999
        result = await agent.execute(devops_task)
        assert result.content["exposed_port"] == 9999


class TestDockerComposeWorker:
    @pytest.mark.asyncio
    async def test_generates_compose_with_services(self, devops_task):
        from services.devops.workers.compose import DockerComposeWorker
        infra = make_infra()
        agent = inject(DockerComposeWorker, infra, "docker_compose_worker")
        result = await agent.execute(devops_task)
        assert result.status == TaskStatus.COMPLETED
        assert len(result.content["services"]) == 3


class TestPipelineConfigWorker:
    @pytest.mark.asyncio
    async def test_generates_workflow(self, devops_task):
        from services.devops.workers.pipeline import PipelineConfigWorker
        infra = make_infra()
        agent = inject(PipelineConfigWorker, infra, "pipeline_config_worker")
        result = await agent.execute(devops_task)
        assert result.status == TaskStatus.COMPLETED
        assert "checkout" in result.content["content"]


class TestEnvironmentConfigWorker:
    @pytest.mark.asyncio
    async def test_generates_env_config(self, devops_task):
        from services.devops.workers.environment import EnvironmentConfigWorker
        infra = make_infra()
        agent = inject(EnvironmentConfigWorker, infra, "environment_config_worker")
        result = await agent.execute(devops_task)
        assert result.status == TaskStatus.COMPLETED
        assert "DATABASE_URL" in result.content["variables"]


class TestProvisionerWorker:
    @pytest.mark.asyncio
    async def test_deploy_success(self, execute_task):
        from services.devops.workers.provisioner import ProvisionerWorker
        infra = make_infra()
        agent = inject(ProvisionerWorker, infra, "provisioner_worker")
        execute_task.context.approved_artifacts["__provider__"] = FakeProvider(deploy_success=True)
        result = await agent.execute(execute_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["success"] is True

    @pytest.mark.asyncio
    async def test_deploy_failure(self, execute_task):
        from services.devops.workers.provisioner import ProvisionerWorker
        infra = make_infra()
        agent = inject(ProvisionerWorker, infra, "provisioner_worker")
        execute_task.context.approved_artifacts["__provider__"] = FakeProvider(deploy_success=False)
        result = await agent.execute(execute_task)
        assert result.status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_works_without_deployment_id(self, execute_task):
        from services.devops.workers.provisioner import ProvisionerWorker
        infra = make_infra()
        agent = inject(ProvisionerWorker, infra, "provisioner_worker")
        execute_task.context.approved_artifacts["__provider__"] = FakeProvider()
        result = await agent.execute(execute_task)
        assert result.status == TaskStatus.COMPLETED


class TestHealthCheckWorker:
    @pytest.mark.asyncio
    async def test_all_checks_pass(self, execute_task):
        from services.devops.workers.health import HealthCheckWorker
        infra = make_infra()
        agent = inject(HealthCheckWorker, infra, "health_check_worker")
        execute_task.context.approved_artifacts["__provider__"] = FakeProvider(health_all_pass=True)
        result = await agent.execute(execute_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["all_passed"] is True

    @pytest.mark.asyncio
    async def test_some_checks_fail(self, execute_task):
        from services.devops.workers.health import HealthCheckWorker
        infra = make_infra()
        agent = inject(HealthCheckWorker, infra, "health_check_worker")
        execute_task.context.approved_artifacts["__provider__"] = FakeProvider(health_all_pass=False)
        result = await agent.execute(execute_task)
        assert result.status == TaskStatus.FAILED
        assert result.content["failed_checks"]


# ══════════════════════════════════════════════════════════════
# LAYER 3d — Integration: leads (fake factory)
# ══════════════════════════════════════════════════════════════

class TestContainerLead:
    @pytest.mark.asyncio
    async def test_success(self, devops_task):
        from services.devops.leads import ContainerLead
        infra = make_infra()
        lead = inject(ContainerLead, infra, "container_lead", layer=4, role="lead")
        factory = FakeFactory({w: ok_result(w) for w in INFRASTRUCTURE_WORKERS})
        devops_task.context.approved_artifacts["__factory__"] = factory
        result = await lead.execute(devops_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["generated"] == len(INFRASTRUCTURE_WORKERS)

    @pytest.mark.asyncio
    async def test_reports_worker_failures(self, devops_task):
        from services.devops.leads import ContainerLead
        infra = make_infra()
        lead = inject(ContainerLead, infra, "container_lead", layer=4, role="lead")
        results = {"dockerfile_writer_worker": fail_result("dockerfile_writer_worker", "boom")}
        devops_task.context.approved_artifacts["__factory__"] = FakeFactory(results)
        result = await lead.execute(devops_task)
        assert result.status == TaskStatus.FAILED


class TestCicdLead:
    @pytest.mark.asyncio
    async def test_success(self, devops_task):
        from services.devops.leads import CicdLead
        infra = make_infra()
        lead = inject(CicdLead, infra, "cicd_lead", layer=4, role="lead")
        factory = FakeFactory({w: ok_result(w) for w in CICD_WORKERS})
        devops_task.context.approved_artifacts["__factory__"] = factory
        result = await lead.execute(devops_task)
        assert result.status == TaskStatus.COMPLETED


class TestInfrastructureOpsLead:
    @pytest.mark.asyncio
    async def test_success(self, execute_task):
        from services.devops.leads import InfrastructureOpsLead
        infra = make_infra()
        lead = inject(InfrastructureOpsLead, infra, "infrastructure_ops_lead", layer=4, role="lead")
        factory = FakeFactory({w: ok_result(w) for w in DEPLOYMENT_WORKERS})
        execute_task.context.approved_artifacts["__factory__"] = factory
        result = await lead.execute(execute_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["executed"] == len(DEPLOYMENT_WORKERS)


# ══════════════════════════════════════════════════════════════
# LAYER 4 — E2E: DevOpsHead full pipeline (fake factory chain)
# ══════════════════════════════════════════════════════════════

@pytest.mark.e2e
class TestDevOpsHeadStageA:
    def _leads_factory(self):
        return FakeFactory({
            "container_lead": ok_result("container_lead", team="infrastructure", generated=2),
            "cicd_lead": ok_result("cicd_lead", team="cicd", generated=2),
        })

    @pytest.mark.asyncio
    async def test_generates_plan_successfully(self, devops_task):
        from services.devops.head import DevOpsHead
        infra = make_infra()
        head = inject(DevOpsHead, infra, "devops_head", layer=3, role="head")
        devops_task.context.approved_artifacts["__factory__"] = self._leads_factory()
        result = await head.execute(devops_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["stage"] == "plan_generated"
        assert result.content["deployment_plan"]["proposed_version"] == "0.1.0"

    @pytest.mark.asyncio
    async def test_escalates_when_source_code_missing(self, devops_task):
        from services.devops.head import DevOpsHead
        infra = make_infra()
        head = inject(DevOpsHead, infra, "devops_head", layer=3, role="head")
        del devops_task.context.approved_artifacts["source_code"]
        devops_task.context.approved_artifacts["__factory__"] = self._leads_factory()
        result = await head.execute(devops_task)
        assert result.status == TaskStatus.ESCALATED

    @pytest.mark.asyncio
    async def test_escalates_when_qa_failed(self, devops_task):
        from services.devops.head import DevOpsHead
        infra = make_infra()
        head = inject(DevOpsHead, infra, "devops_head", layer=3, role="head")
        devops_task.context.approved_artifacts["qa_report"] = {"verdict": "fail", "blocking_conditions": ["x"]}
        devops_task.context.approved_artifacts["__factory__"] = self._leads_factory()
        result = await head.execute(devops_task)
        assert result.status == TaskStatus.ESCALATED

    @pytest.mark.asyncio
    async def test_escalates_when_security_failed(self, devops_task):
        from services.devops.head import DevOpsHead
        infra = make_infra()
        head = inject(DevOpsHead, infra, "devops_head", layer=3, role="head")
        devops_task.context.approved_artifacts["security_report"] = {"verdict": "fail", "blocking_conditions": ["y"]}
        devops_task.context.approved_artifacts["__factory__"] = self._leads_factory()
        result = await head.execute(devops_task)
        assert result.status == TaskStatus.ESCALATED

    @pytest.mark.asyncio
    async def test_plan_includes_artifact_refs(self, devops_task):
        from services.devops.head import DevOpsHead
        infra = make_infra()
        head = inject(DevOpsHead, infra, "devops_head", layer=3, role="head")
        devops_task.context.approved_artifacts["__factory__"] = self._leads_factory()
        result = await head.execute(devops_task)
        artifact_types = [a.artifact_type for a in result.artifacts]
        assert "deployment_plan" in artifact_types

    @pytest.mark.asyncio
    async def test_publishes_approval_required_ws_event(self, devops_task):
        from services.devops.head import DevOpsHead
        infra = make_infra()
        head = inject(DevOpsHead, infra, "devops_head", layer=3, role="head")
        devops_task.context.approved_artifacts["__factory__"] = self._leads_factory()
        result = await head.execute(devops_task)
        assert any(e.event_type == "approval_required" for e in result.ws_events)

    @pytest.mark.asyncio
    async def test_fails_when_infra_lead_fails(self, devops_task):
        from services.devops.head import DevOpsHead
        infra = make_infra()
        head = inject(DevOpsHead, infra, "devops_head", layer=3, role="head")
        devops_task.context.approved_artifacts["__factory__"] = FakeFactory({
            "container_lead": fail_result("container_lead", "docker build config invalid"),
        })
        result = await head.execute(devops_task)
        assert result.status == TaskStatus.ESCALATED


@pytest.mark.e2e
class TestDevOpsHeadStageB:
    def _deploy_factory(self, health_pass=True, deploy_success=True):
        content = {
            "success": deploy_success, "deployment_ref": "fake::p",
            "checks": [{"check_name": n, "passed": health_pass, "detail": "ok"} for n in REQUIRED_HEALTH_CHECKS],
        }
        return FakeFactory({
            "infrastructure_ops_lead": ok_result("infrastructure_ops_lead", team="deployment", executed=2),
        })

    def _seed_execution_context(self, task, health_pass=True, deploy_success=True):
        task.context.approved_artifacts["provisioner_worker"] = {
            "success": deploy_success, "deployment_ref": "fake::p", "detail": "ok",
        }
        task.context.approved_artifacts["health_check_worker"] = {
            "checks": [{"check_name": n, "passed": health_pass, "detail": "ok"} for n in REQUIRED_HEALTH_CHECKS],
            "all_passed": health_pass,
        }

    @pytest.mark.asyncio
    async def test_successful_deployment_releases(self, execute_task):
        from services.devops.head import DevOpsHead
        infra = make_infra()
        head = inject(DevOpsHead, infra, "devops_head", layer=3, role="head")
        execute_task.context.approved_artifacts["__factory__"] = self._deploy_factory()
        execute_task.context.approved_artifacts["__provider__"] = FakeProvider(deploy_success=True, health_all_pass=True)
        self._seed_execution_context(execute_task, health_pass=True, deploy_success=True)
        result = await head.execute(execute_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_health_failure_triggers_rollback(self, execute_task):
        from services.devops.head import DevOpsHead
        infra = make_infra()
        head = inject(DevOpsHead, infra, "devops_head", layer=3, role="head")
        execute_task.context.approved_artifacts["__factory__"] = self._deploy_factory()
        execute_task.context.approved_artifacts["__provider__"] = FakeProvider(deploy_success=True, health_all_pass=False)
        self._seed_execution_context(execute_task, health_pass=False, deploy_success=True)
        result = await head.execute(execute_task)
        assert result.status == TaskStatus.FAILED
        assert result.content["stage"] == "rolled_back"

    @pytest.mark.asyncio
    async def test_deploy_failure_triggers_rollback(self, execute_task):
        from services.devops.head import DevOpsHead
        infra = make_infra()
        head = inject(DevOpsHead, infra, "devops_head", layer=3, role="head")
        execute_task.context.approved_artifacts["__factory__"] = self._deploy_factory()
        execute_task.context.approved_artifacts["__provider__"] = FakeProvider(deploy_success=False)
        self._seed_execution_context(execute_task, health_pass=True, deploy_success=False)
        result = await head.execute(execute_task)
        assert result.status == TaskStatus.FAILED
        assert "deployment_failure" in result.failure_reason

    @pytest.mark.asyncio
    async def test_escalates_without_deployment_plan(self, project_id, devops_context):
        from services.devops.head import DevOpsHead
        infra = make_infra()
        head = inject(DevOpsHead, infra, "devops_head", layer=3, role="head")
        task = TaskInput(task_id=str(uuid.uuid4()), project_id=project_id, agent_id="devops_head",
                          parent_agent_id="manager_agent", task_type="execute_deployment",
                          description="x", expected_output="y", context=devops_context)
        result = await head.execute(task)
        assert result.status == TaskStatus.ESCALATED

    @pytest.mark.asyncio
    async def test_publishes_devops_phase_completed_on_success(self, execute_task):
        from services.devops.head import DevOpsHead
        infra = make_infra()
        head = inject(DevOpsHead, infra, "devops_head", layer=3, role="head")
        execute_task.context.approved_artifacts["__factory__"] = self._deploy_factory()
        execute_task.context.approved_artifacts["__provider__"] = FakeProvider()
        self._seed_execution_context(execute_task, health_pass=True, deploy_success=True)
        result = await head.execute(execute_task)
        assert any(e.subject == "devops.phase.completed" for e in result.nats_events)

    @pytest.mark.asyncio
    async def test_publishes_devops_phase_completed_on_rollback_too(self, execute_task):
        """Per spec's Publish list, devops.phase.completed fires even on
        a rolled-back deployment — the phase finished processing, even
        though the deployment itself failed."""
        from services.devops.head import DevOpsHead
        infra = make_infra()
        head = inject(DevOpsHead, infra, "devops_head", layer=3, role="head")
        execute_task.context.approved_artifacts["__factory__"] = self._deploy_factory()
        execute_task.context.approved_artifacts["__provider__"] = FakeProvider(health_all_pass=False)
        self._seed_execution_context(execute_task, health_pass=False, deploy_success=True)
        result = await head.execute(execute_task)
        assert any(e.subject == "devops.phase.completed" for e in result.nats_events)

    @pytest.mark.asyncio
    async def test_creates_deployment_report_artifact(self, execute_task):
        from services.devops.head import DevOpsHead
        infra = make_infra()
        head = inject(DevOpsHead, infra, "devops_head", layer=3, role="head")
        execute_task.context.approved_artifacts["__factory__"] = self._deploy_factory()
        execute_task.context.approved_artifacts["__provider__"] = FakeProvider()
        self._seed_execution_context(execute_task, health_pass=True, deploy_success=True)
        result = await head.execute(execute_task)
        artifact_types = [a.artifact_type for a in result.artifacts]
        assert "deployment_report" in artifact_types

    @pytest.mark.asyncio
    async def test_rollback_creates_rollback_report_artifact(self, execute_task):
        from services.devops.head import DevOpsHead
        infra = make_infra()
        head = inject(DevOpsHead, infra, "devops_head", layer=3, role="head")
        execute_task.context.approved_artifacts["__factory__"] = self._deploy_factory()
        execute_task.context.approved_artifacts["__provider__"] = FakeProvider(health_all_pass=False)
        self._seed_execution_context(execute_task, health_pass=False, deploy_success=True)
        result = await head.execute(execute_task)
        artifact_types = [a.artifact_type for a in result.artifacts]
        assert "rollback_report" in artifact_types

    @pytest.mark.asyncio
    async def test_devops_never_writes_source_code(self):
        """Sanity check: DevOpsHead module never references a source-code
        write method — Engineering remains the sole code producer."""
        import services.devops.head as head_module
        assert not hasattr(head_module, "commit_files")
        assert not hasattr(head_module, "write_source_code")


# ══════════════════════════════════════════════════════════════
# LAYER 4b — E2E: manager wiring sanity (Stage A -> Stage B linkage)
# ══════════════════════════════════════════════════════════════

class TestManagerDevOpsWiring:
    """
    Confirms the M3.6 orchestration-gap fixes documented in
    docs/M3.6_DevOps_Service_Handover.md are present in Manager Service.
    """

    def test_execute_deployment_task_type_maps_to_devops(self):
        from services.manager.graphs.delegation import TASK_DEPARTMENT_MAP
        assert TASK_DEPARTMENT_MAP.get("execute_deployment") == "devops"

    def test_manager_main_has_deployment_plan_branch(self):
        import inspect
        import services.manager.main as manager_main
        src = inspect.getsource(manager_main)
        assert 'artifact_type == "deployment_plan"' in src
        assert "execute_deployment" in src

    def test_manager_main_marks_artifact_approved(self):
        import inspect
        import services.manager.main as manager_main
        src = inspect.getsource(manager_main)
        assert "update_status" in src

    def test_department_artifact_types_has_devops_entry(self):
        import inspect
        import services.manager.main as manager_main
        src = inspect.getsource(manager_main)
        assert '"devops":' in src
        assert '"qa_report"' in src and '"security_report"' in src and '"deployment_plan"' in src


# ══════════════════════════════════════════════════════════════
# LAYER 1g — Unit: schemas / error hierarchy
# ══════════════════════════════════════════════════════════════

class TestDevOpsSchemas:
    def test_deploy_request_defaults_feature_name(self):
        from services.devops.schemas import DeployRequest
        req = DeployRequest(project_id="p", workflow_id="w")
        assert req.feature_name == "default"

    def test_rollback_request_requires_reason(self):
        from services.devops.schemas import RollbackRequest
        req = RollbackRequest(project_id="p", reason="health check failed")
        assert req.reason == "health check failed"

    def test_approve_deployment_request_optional_fields(self):
        from services.devops.schemas import ApproveDeploymentRequest
        req = ApproveDeploymentRequest(project_id="p", approved=True)
        assert req.approved_by is None and req.feedback is None

    def test_devops_task_response_roundtrip(self):
        from services.devops.schemas import DevOpsTaskResponse
        resp = DevOpsTaskResponse(task_id="t1", team="infrastructure", worker_agent_id="dockerfile_writer_worker",
                                   status=DevOpsTaskStatus.PENDING, retry_count=0, depends_on=[])
        assert resp.task_id == "t1"

    def test_deployment_status_response_defaults(self):
        from services.devops.schemas import DeploymentStatusResponse
        resp = DeploymentStatusResponse(project_id="p", phase_status="running")
        assert resp.status == DeploymentStatus.PENDING

    def test_devops_completed_event_shape(self):
        from services.devops.schemas import DevOpsCompletedEvent
        evt = DevOpsCompletedEvent(project_id="p", workflow_id="w", feature_name="f",
                                    passed=True, status="healthy", version="1.0.0")
        assert evt.passed is True

    def test_error_hierarchy(self):
        from services.devops.schemas import (
            DeadLetterError, DeploymentBlockedError, DevOpsServiceError,
            HealthCheckFailedError, NoValidatedArtifactsError, RollbackFailedError,
        )
        for exc_cls in (NoValidatedArtifactsError, DeploymentBlockedError,
                        HealthCheckFailedError, RollbackFailedError, DeadLetterError):
            assert issubclass(exc_cls, DevOpsServiceError)

    def test_no_validated_artifacts_error_raisable(self):
        from services.devops.schemas import NoValidatedArtifactsError
        with pytest.raises(NoValidatedArtifactsError):
            raise NoValidatedArtifactsError("missing qa_report")

    def test_health_check_failed_error_raisable(self):
        from services.devops.schemas import HealthCheckFailedError
        with pytest.raises(HealthCheckFailedError):
            raise HealthCheckFailedError("database_connected failed")

    def test_rollback_failed_error_raisable(self):
        from services.devops.schemas import RollbackFailedError
        with pytest.raises(RollbackFailedError):
            raise RollbackFailedError("manual intervention required")


# ══════════════════════════════════════════════════════════════
# LAYER 1h — Unit: additional model / enum coverage
# ══════════════════════════════════════════════════════════════

class TestEnums:
    def test_deployment_status_values(self):
        assert DeploymentStatus.PENDING.value == "pending"
        assert DeploymentStatus.AWAITING_APPROVAL.value == "awaiting_approval"
        assert DeploymentStatus.HEALTHY.value == "healthy"
        assert DeploymentStatus.ROLLED_BACK.value == "rolled_back"

    def test_worker_team_values(self):
        assert WorkerTeam.INFRASTRUCTURE.value == "infrastructure"
        assert WorkerTeam.CICD.value == "cicd"
        assert WorkerTeam.DEPLOYMENT.value == "deployment"

    def test_version_bump_values(self):
        assert VersionBump.MAJOR.value == "major"
        assert VersionBump.MINOR.value == "minor"
        assert VersionBump.PATCH.value == "patch"

    def test_rollback_status_values(self):
        assert RollbackStatus.INITIATED.value == "initiated"
        assert RollbackStatus.COMPLETED.value == "completed"
        assert RollbackStatus.FAILED.value == "failed"

    def test_devops_task_status_values(self):
        assert DevOpsTaskStatus.PENDING.value == "pending"
        assert DevOpsTaskStatus.DEAD_LETTERED.value == "dead_lettered"


class TestModelSerialization:
    def test_deployment_plan_model_dump_roundtrip(self, project_id):
        plan = DeploymentPlan(project_id=project_id, proposed_version="1.0.0")
        dumped = plan.model_dump()
        restored = DeploymentPlan(**dumped)
        assert restored.proposed_version == "1.0.0"

    def test_health_report_model_dump_roundtrip(self, project_id):
        hr = HealthReport(project_id=project_id, deployment_id="d",
                           checks=[HealthCheckResult(check_name="x", passed=True)])
        dumped = hr.model_dump()
        restored = HealthReport(**dumped)
        assert restored.all_passed is True

    def test_deployment_report_model_dump_roundtrip(self, project_id):
        report = DeploymentReport(project_id=project_id, deployment_id="d", status=DeploymentStatus.HEALTHY)
        dumped = report.model_dump()
        restored = DeploymentReport(**dumped)
        assert restored.succeeded is True

    def test_devops_plan_model_dump_roundtrip(self, project_id):
        plan = build_devops_plan(project_id, "f", {})
        dumped = plan.model_dump()
        restored = DevOpsPlan(**dumped)
        assert len(restored.tasks) == len(plan.tasks)


# ══════════════════════════════════════════════════════════════
# LAYER 3e — Integration: deployment_repository additional methods
# ══════════════════════════════════════════════════════════════

class TestDeploymentRepositoryAdditional:
    @pytest.mark.asyncio
    async def test_update_status_deploying_sets_started_at(self):
        from services.devops.integration.deployment_repository import DeploymentRepository
        db = MagicMock(execute=AsyncMock())
        await DeploymentRepository.update_status(db, "d1", "deploying")
        db.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_status_healthy_sets_completed_at(self):
        from services.devops.integration.deployment_repository import DeploymentRepository
        db = MagicMock(execute=AsyncMock())
        await DeploymentRepository.update_status(db, "d1", "healthy")
        db.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_status_with_failure_reason(self):
        from services.devops.integration.deployment_repository import DeploymentRepository
        db = MagicMock(execute=AsyncMock())
        await DeploymentRepository.update_status(db, "d1", "failed", failure_reason="oops")
        db.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_by_id_returns_none_when_missing(self):
        from services.devops.integration.deployment_repository import DeploymentRepository
        db = MagicMock(execute=AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))))
        result = await DeploymentRepository.get_by_id(db, "missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_latest_for_project_returns_none_when_empty(self):
        from services.devops.integration.deployment_repository import DeploymentRepository
        db = MagicMock(execute=AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))))
        result = await DeploymentRepository.get_latest_for_project(db, "p1")
        assert result is None

    @pytest.mark.asyncio
    async def test_deployment_history_list_for_deployment(self):
        from services.devops.integration.deployment_repository import DeploymentHistoryRepository
        db = MagicMock(execute=AsyncMock(return_value=MagicMock(scalars=MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=[]))))))
        result = await DeploymentHistoryRepository.list_for_deployment(db, "d1")
        assert result == []

    @pytest.mark.asyncio
    async def test_deployment_health_list_for_deployment(self):
        from services.devops.integration.deployment_repository import DeploymentHealthRepository
        db = MagicMock(execute=AsyncMock(return_value=MagicMock(scalars=MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=[]))))))
        result = await DeploymentHealthRepository.list_for_deployment(db, "d1")
        assert result == []

    @pytest.mark.asyncio
    async def test_release_metadata_get_latest_returns_none_when_empty(self):
        from services.devops.integration.deployment_repository import ReleaseMetadataRepository
        db = MagicMock(execute=AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))))
        result = await ReleaseMetadataRepository.get_latest_for_project(db, "p1")
        assert result is None

    @pytest.mark.asyncio
    async def test_rollback_record_mark_completed(self):
        from services.devops.integration.deployment_repository import RollbackRecordRepository
        db = MagicMock(execute=AsyncMock())
        await RollbackRecordRepository.mark_completed(db, "rb1")
        db.execute.assert_awaited_once()


# ══════════════════════════════════════════════════════════════
# LAYER 3f — Integration: additional worker edge cases
# ══════════════════════════════════════════════════════════════

class TestWorkerEdgeCases:
    @pytest.mark.asyncio
    async def test_environment_config_worker_without_database_schema(self, devops_task):
        from services.devops.workers.environment import EnvironmentConfigWorker
        infra = make_infra()
        agent = inject(EnvironmentConfigWorker, infra, "environment_config_worker")
        del devops_task.context.approved_artifacts["database_schema"]
        result = await agent.execute(devops_task)
        assert result.status == TaskStatus.COMPLETED
        assert "DB_POOL_SIZE" not in result.content["variables"]

    @pytest.mark.asyncio
    async def test_dockerfile_worker_node_stack(self, devops_task):
        from services.devops.workers.dockerfile import DockerfileWriterWorker
        infra = make_infra()
        agent = inject(DockerfileWriterWorker, infra, "dockerfile_writer_worker")
        devops_task.context.tech_stack = {"backend": "Node+Express"}
        result = await agent.execute(devops_task)
        assert "node" in result.content["content"]

    @pytest.mark.asyncio
    async def test_provisioner_worker_uses_default_provider_when_none_set(self, execute_task):
        from services.devops.workers.provisioner import ProvisionerWorker
        infra = make_infra()
        agent = inject(ProvisionerWorker, infra, "provisioner_worker")
        # No __provider__ override -> falls back to default_provider() (DockerComposeProvider)
        result = await agent.execute(execute_task)
        assert result.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)

    @pytest.mark.asyncio
    async def test_health_check_worker_uses_default_provider_when_none_set(self, execute_task):
        from services.devops.workers.health import HealthCheckWorker
        infra = make_infra()
        agent = inject(HealthCheckWorker, infra, "health_check_worker")
        result = await agent.execute(execute_task)
        assert result.status == TaskStatus.COMPLETED
