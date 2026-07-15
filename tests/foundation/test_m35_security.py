"""
tests/foundation/test_m35_security.py
========================================
M3.5 Security Service tests — 4 layers matching the M3.1-M3.4 pattern.

Layer 1 — Unit:        models, utils, task decomposition, validation-gate
                       logic (classify_findings/build_security_report/
                       build_risk_assessment), routing predicates, agent
                       registry verification
Layer 2 — Graph:       LangGraph node functions + graph construction
Layer 3 — Integration: workers (mocked LLM / deterministic), read-only
                       Repository Service client (mocked httpx), leads
                       (fake factory)
Layer 4 — E2E:         full SecurityHead pipeline (fake factory chain)
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

from services.security.models import (
    CodeIssue,
    ComplianceReport,
    DependencyEntry,
    DependencyManifest,
    DependencyScan,
    FindingCategory,
    FindingSeverity,
    LicenseReport,
    RetryRequest,
    RiskAssessment,
    SBOM,
    SBOMComponent,
    ScanTeam,
    SecretHit,
    SecretScan,
    SecurityFinding,
    SecurityPlan,
    SecurityReport,
    SecurityTask,
    SecurityTaskStatus,
    SecurityVerdict,
    StaticAnalysisReport,
    Vulnerability,
)
from services.security.utils import (
    KNOWN_VULNERABLE_PACKAGES,
    LICENSE_TABLE,
    classify_license,
    exponential_backoff_seconds,
    extract_dependencies_from_source,
    finding_id_for,
    idempotency_key,
    parse_llm_json,
    quality_gate,
    scan_content_for_secrets,
    severity_for_category,
)
from services.security.context import (
    CODE_WORKERS,
    COMPLIANCE_WORKERS,
    DEFAULT_RISK_THRESHOLD,
    DEPENDENCY_WORKERS,
    build_retry_request,
    build_risk_assessment,
    build_security_plan,
    build_security_report,
    classify_findings,
    team_progress,
    topological_batches,
)
from services.security.routing import (
    MAX_RETRY_CYCLES,
    MAX_TASK_RETRIES,
    route_after_aggregate,
    route_after_fan_out,
    route_after_risk_classification,
    route_after_security_findings,
    route_after_static_analysis,
    route_after_validate_inputs,
    route_checkpoint_recovery,
    route_task_retry,
)
from services.security.integration.repository_client import (
    RepositoryServiceClientError,
    SecurityRepositoryReadClient,
)
from services.security.schemas import SecurityServiceError


# ══════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════

@pytest.fixture
def project_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def security_context(project_id) -> AgentContext:
    return AgentContext(
        project_id=project_id, workflow_id=str(uuid.uuid4()),
        current_phase=6, project_name="TestApp",
        project_description="A test SaaS app.",
        approved_artifacts={
            "source_code": {"files": [
                {"path": "app/main.py", "language": "python", "content": "x = 1"},
                {"path": "requirements.txt", "language": "text",
                 "content": "requests==2.19.0\npyyaml==5.2\nflask==2.0.0"},
            ]},
            "openapi_spec": {"paths": {"/health": {}, "/auth/login": {}, "/users": {}}},
            "database_schema": {"tables": [{"name": "users"}]},
            "security_architecture": {"auth": "JWT+RBAC"},
        },
        tech_stack={"backend": "Python+FastAPI", "frontend": "React+TS"},
        llm_provider="anthropic", llm_model="claude-sonnet-4-6",
        budget_limit_usd=50.0, total_spend_usd=2.0,
    )


@pytest.fixture
def security_context_missing_inputs(security_context) -> AgentContext:
    security_context.approved_artifacts = {
        k: v for k, v in security_context.approved_artifacts.items() if k != "source_code"
    }
    return security_context


@pytest.fixture
def security_task(project_id, security_context) -> TaskInput:
    return TaskInput(
        task_id=str(uuid.uuid4()), project_id=project_id,
        agent_id="security_head", parent_agent_id="manager_agent",
        task_type="run_security_pipeline",
        description="Validate approved Engineering output for security risk",
        expected_output="SecurityReport with verdict pass/warn/fail",
        context=security_context,
    )


@pytest.fixture
def security_task_missing_inputs(project_id, security_context_missing_inputs) -> TaskInput:
    return TaskInput(
        task_id=str(uuid.uuid4()), project_id=project_id,
        agent_id="security_head", parent_agent_id="manager_agent",
        task_type="run_security_pipeline",
        description="Validate approved Engineering output (missing inputs)",
        expected_output="SecurityReport with verdict pass/warn/fail",
        context=security_context_missing_inputs,
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
    a.department = "security"
    a.layer = layer
    a.role = role
    a.responsibilities = ["Security validation"]
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
                        content=content or {"finding_count": 0},
                        quality_score=0.9,
                        artifacts=[{"artifact_id": "a1",
                                    "artifact_type": content.get("__artifact_type__", "dependency_scan"),
                                    "version": 1}])


def fail_result(agent_id: str, reason: str) -> AgentResult:
    return AgentResult(task_id="t", agent_id=agent_id, status=TaskStatus.FAILED,
                        content={}, quality_score=0.0, failure_reason=reason)


# ══════════════════════════════════════════════════════════════
# LAYER 1a — Unit: models
# ══════════════════════════════════════════════════════════════

class TestDependencyModels:
    def test_dependency_entry_defaults(self):
        d = DependencyEntry(name="requests")
        assert d.version == "unknown" and d.ecosystem == "unknown"

    def test_dependency_manifest_holds_entries(self):
        m = DependencyManifest(project_id="p", dependencies=[DependencyEntry(name="requests")])
        assert len(m.dependencies) == 1


class TestDependencyScan:
    def test_critical_count_zero_without_vulns(self):
        s = DependencyScan(project_id="p")
        assert s.critical_count == 0 and s.has_critical is False

    def test_critical_count_with_critical_vuln(self):
        s = DependencyScan(project_id="p", vulnerabilities=[
            Vulnerability(package="django", cve_id="CVE-1", severity=FindingSeverity.CRITICAL)])
        assert s.critical_count == 1 and s.has_critical is True

    def test_high_count(self):
        s = DependencyScan(project_id="p", vulnerabilities=[
            Vulnerability(package="lodash", cve_id="CVE-2", severity=FindingSeverity.HIGH),
            Vulnerability(package="lodash2", cve_id="CVE-3", severity=FindingSeverity.HIGH),
        ])
        assert s.high_count == 2

    def test_has_critical_false_with_only_medium(self):
        s = DependencyScan(project_id="p", vulnerabilities=[
            Vulnerability(package="requests", cve_id="CVE-4", severity=FindingSeverity.MEDIUM)])
        assert s.has_critical is False


class TestStaticAnalysisReport:
    def test_all_findings_combines_both_lists(self):
        r = StaticAnalysisReport(
            project_id="p",
            owasp_findings=[CodeIssue(rule="a", severity=FindingSeverity.LOW)],
            injection_findings=[CodeIssue(rule="b", severity=FindingSeverity.HIGH)],
        )
        assert len(r.all_findings) == 2

    def test_has_critical_true_with_critical_owasp(self):
        r = StaticAnalysisReport(project_id="p", owasp_findings=[
            CodeIssue(rule="a", severity=FindingSeverity.CRITICAL)])
        assert r.has_critical is True

    def test_has_critical_false_when_none_critical(self):
        r = StaticAnalysisReport(project_id="p", owasp_findings=[
            CodeIssue(rule="a", severity=FindingSeverity.MEDIUM)])
        assert r.has_critical is False


class TestSecretScan:
    def test_secret_count_zero_by_default(self):
        s = SecretScan(project_id="p")
        assert s.secret_count == 0 and s.has_secrets is False

    def test_secret_count_with_hits(self):
        s = SecretScan(project_id="p", secrets=[SecretHit(file="a.py", rule="aws_access_key", line=3)])
        assert s.secret_count == 1 and s.has_secrets is True


class TestSBOM:
    def test_component_count(self):
        sbom = SBOM(project_id="p", components=[
            SBOMComponent(name="requests"), SBOMComponent(name="flask")])
        assert sbom.component_count == 2

    def test_default_format(self):
        sbom = SBOM(project_id="p")
        assert sbom.format == "CycloneDX-lite"


class TestLicenseReport:
    def test_compliant_true_without_disallowed(self):
        r = LicenseReport(project_id="p", licenses_found={"MIT": 3})
        assert r.compliant is True

    def test_compliant_false_with_disallowed(self):
        r = LicenseReport(project_id="p", disallowed_licenses=["GPL-3.0"])
        assert r.compliant is False


class TestComplianceReport:
    def test_compliant_true_without_violations(self):
        r = ComplianceReport(project_id="p", checklist={"a": True})
        assert r.compliant is True

    def test_compliant_false_with_violations(self):
        r = ComplianceReport(project_id="p", violations=["missing_review"])
        assert r.compliant is False


class TestRiskAssessment:
    def test_defaults(self):
        r = RiskAssessment(project_id="p")
        assert r.risk_level == "low" and r.risk_score == 0.0


class TestSecurityFinding:
    def test_is_blocking_for_critical(self):
        f = SecurityFinding(project_id="p", category=FindingCategory.SECRET,
                             severity=FindingSeverity.CRITICAL, description="x", source_worker="w")
        assert f.is_blocking is True

    def test_is_blocking_for_high(self):
        f = SecurityFinding(project_id="p", category=FindingCategory.CVE,
                             severity=FindingSeverity.HIGH, description="x", source_worker="w")
        assert f.is_blocking is True

    def test_not_blocking_for_medium(self):
        f = SecurityFinding(project_id="p", category=FindingCategory.CVE,
                             severity=FindingSeverity.MEDIUM, description="x", source_worker="w")
        assert f.is_blocking is False

    def test_not_blocking_for_low(self):
        f = SecurityFinding(project_id="p", category=FindingCategory.CVE,
                             severity=FindingSeverity.LOW, description="x", source_worker="w")
        assert f.is_blocking is False


class TestRetryRequest:
    def test_can_retry_true_below_max(self):
        r = RetryRequest(project_id="p", target_team="engineering", reason="x", retry_count=1, max_retries=3)
        assert r.can_retry is True

    def test_can_retry_false_at_max(self):
        r = RetryRequest(project_id="p", target_team="engineering", reason="x", retry_count=3, max_retries=3)
        assert r.can_retry is False


class TestSecurityTask:
    def test_can_run_with_no_dependencies(self):
        t = SecurityTask(project_id="p", team=ScanTeam.DEPENDENCY, worker_agent_id="w")
        assert t.can_run(set()) is True

    def test_can_run_false_when_dependency_incomplete(self):
        t = SecurityTask(project_id="p", team=ScanTeam.DEPENDENCY, worker_agent_id="w", depends_on=["dep-1"])
        assert t.can_run(set()) is False

    def test_can_run_true_when_dependency_complete(self):
        t = SecurityTask(project_id="p", team=ScanTeam.DEPENDENCY, worker_agent_id="w", depends_on=["dep-1"])
        assert t.can_run({"dep-1"}) is True

    def test_can_run_false_when_not_pending(self):
        t = SecurityTask(project_id="p", team=ScanTeam.DEPENDENCY, worker_agent_id="w",
                          status=SecurityTaskStatus.RUNNING)
        assert t.can_run(set()) is False

    @pytest.mark.parametrize("retries,expected", [(0, 1), (1, 2), (2, 4), (3, 8), (10, 60)])
    def test_backoff_seconds_exponential_capped(self, retries, expected):
        t = SecurityTask(project_id="p", team=ScanTeam.DEPENDENCY, worker_agent_id="w", retry_count=retries)
        assert t.next_backoff_seconds() == expected


class TestSecurityPlan:
    def _plan(self):
        t1 = SecurityTask(project_id="p", team=ScanTeam.DEPENDENCY, worker_agent_id="cve_scanner_worker")
        t2 = SecurityTask(project_id="p", team=ScanTeam.CODE, worker_agent_id="owasp_checker_worker",
                           depends_on=[t1.task_id])
        t3 = SecurityTask(project_id="p", team=ScanTeam.COMPLIANCE, worker_agent_id="compliance_validator_worker",
                           status=SecurityTaskStatus.COMPLETED)
        return SecurityPlan(project_id="p", feature_name="f", tasks=[t1, t2, t3]), t1, t2, t3

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
        assert plan.tasks_by_team(ScanTeam.DEPENDENCY) == [t1]
        assert plan.tasks_by_team(ScanTeam.CODE) == [t2]

    def test_all_complete_false_when_pending_tasks_remain(self):
        plan, *_ = self._plan()
        assert plan.all_complete is False

    def test_all_complete_true_when_all_completed(self):
        plan, t1, t2, t3 = self._plan()
        t1.status = SecurityTaskStatus.COMPLETED
        t2.status = SecurityTaskStatus.COMPLETED
        assert plan.all_complete is True

    def test_any_dead_lettered_false_by_default(self):
        plan, *_ = self._plan()
        assert plan.any_dead_lettered is False

    def test_any_dead_lettered_true_when_flagged(self):
        plan, t1, t2, t3 = self._plan()
        t2.dead_lettered = True
        assert plan.any_dead_lettered is True


class TestSecurityReport:
    def test_defaults_to_fail(self):
        r = SecurityReport(project_id="p")
        assert r.verdict == SecurityVerdict.FAIL


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
        assert idempotency_key("p", "t", "w") == idempotency_key("p", "t", "w")

    def test_differs_for_different_inputs(self):
        assert idempotency_key("p", "t", "w1") != idempotency_key("p", "t", "w2")

    def test_key_length(self):
        assert len(idempotency_key("p", "t", "w")) == 16


class TestFindingIdFor:
    def test_deterministic(self):
        assert finding_id_for("p", "secret", "m1") == finding_id_for("p", "secret", "m1")

    def test_differs_by_category(self):
        assert finding_id_for("p", "secret", "m1") != finding_id_for("p", "cve", "m1")

    def test_handles_missing_module_id(self):
        assert finding_id_for("p", "secret", None)


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


class TestSeverityForCategory:
    @pytest.mark.parametrize("category,expected", [
        ("secret", "critical"),
        ("cve", "high"),
        ("compliance_violation", "high"),
        ("high_risk_dependency", "high"),
        ("owasp", "medium"),
        ("license", "medium"),
        ("unknown_category", "medium"),
    ])
    def test_severity_mapping(self, category, expected):
        assert severity_for_category(category) == expected


class TestExtractDependenciesFromSource:
    def test_extracts_requirements_txt_pinned_versions(self):
        files = [{"path": "requirements.txt", "content": "requests==2.19.0\npyyaml==5.2\n"}]
        deps = extract_dependencies_from_source(files)
        names = {d["name"] for d in deps}
        assert names == {"requests", "pyyaml"}
        assert all(d["ecosystem"] == "pypi" for d in deps)

    def test_ignores_comment_and_blank_lines(self):
        files = [{"path": "requirements.txt", "content": "# comment\n\nrequests==2.19.0\n"}]
        deps = extract_dependencies_from_source(files)
        assert len(deps) == 1

    def test_extracts_package_json_dependencies(self):
        content = '{\n  "dependencies": {\n    "lodash": "^4.17.15",\n    "express": "~4.16.0"\n  }\n}'
        files = [{"path": "package.json", "content": content}]
        deps = extract_dependencies_from_source(files)
        names = {d["name"] for d in deps}
        assert names == {"lodash", "express"}
        assert all(d["ecosystem"] == "npm" for d in deps)

    def test_strips_caret_and_tilde_prefixes(self):
        content = '{\n  "dependencies": {\n    "lodash": "^4.17.15"\n  }\n}'
        files = [{"path": "package.json", "content": content}]
        deps = extract_dependencies_from_source(files)
        assert deps[0]["version"] == "4.17.15"

    def test_returns_empty_for_no_manifest_files(self):
        files = [{"path": "app/main.py", "content": "x = 1"}]
        assert extract_dependencies_from_source(files) == []

    def test_deduplicates_repeated_entries(self):
        files = [{"path": "requirements.txt", "content": "requests==2.19.0\nrequests==2.19.0\n"}]
        deps = extract_dependencies_from_source(files)
        assert len(deps) == 1

    def test_handles_multiple_manifest_files(self):
        files = [
            {"path": "requirements.txt", "content": "requests==2.19.0\n"},
            {"path": "frontend/package.json", "content": '{"dependencies": {"lodash": "^4.17.15"}}'},
        ]
        deps = extract_dependencies_from_source(files)
        assert len(deps) == 2


class TestScanContentForSecrets:
    def test_detects_aws_access_key(self):
        hits = scan_content_for_secrets("a.py", "key = 'AKIAABCDEFGHIJKLMNOP'")
        assert any(h["rule"] == "aws_access_key" for h in hits)

    def test_detects_private_key_header(self):
        hits = scan_content_for_secrets("id_rsa", "-----BEGIN RSA PRIVATE KEY-----")
        assert any(h["rule"] == "private_key" for h in hits)

    def test_detects_generic_api_key_assignment(self):
        hits = scan_content_for_secrets("config.py", 'api_key = "abcdefghijklmnopqrstuvwx"')
        assert any(h["rule"] == "generic_api_key" for h in hits)

    def test_detects_slack_token(self):
        hits = scan_content_for_secrets("bot.py", "token = 'xoxb-1234567890-abcdefgh'")
        assert any(h["rule"] == "slack_token" for h in hits)

    def test_clean_content_produces_no_hits(self):
        hits = scan_content_for_secrets("a.py", "def add(a, b):\n    return a + b\n")
        assert hits == []

    def test_reports_correct_line_number(self):
        content = "line1\nline2\nkey = 'AKIAABCDEFGHIJKLMNOP'\n"
        hits = scan_content_for_secrets("a.py", content)
        assert hits[0]["line"] == 3


class TestClassifyLicense:
    def test_permissive_license(self):
        assert classify_license("MIT") == "permissive"

    def test_disallowed_license(self):
        assert classify_license("GPL-3.0") == "disallowed"

    def test_unknown_license(self):
        assert classify_license("Some-Custom-License") == "unknown"


class TestKnownTables:
    def test_known_vulnerable_packages_have_required_fields(self):
        for name, entry in KNOWN_VULNERABLE_PACKAGES.items():
            assert {"cve_id", "severity", "max_safe_version"} <= set(entry.keys())

    def test_license_table_has_some_gpl_entry_for_gate_testing(self):
        assert LICENSE_TABLE.get("some-gpl-lib") == "GPL-3.0"


# ══════════════════════════════════════════════════════════════
# LAYER 1c — Unit: task decomposition + validation-gate logic
# ══════════════════════════════════════════════════════════════

class TestBuildSecurityPlan:
    def test_includes_all_three_teams(self, project_id):
        plan = build_security_plan(project_id, "f", {"source_code": {}})
        teams = {t.team for t in plan.tasks}
        assert teams == {ScanTeam.DEPENDENCY, ScanTeam.CODE, ScanTeam.COMPLIANCE}

    def test_worker_count_matches_registry(self, project_id):
        plan = build_security_plan(project_id, "f", {})
        expected = len(DEPENDENCY_WORKERS) + len(CODE_WORKERS) + len(COMPLIANCE_WORKERS)
        assert len(plan.tasks) == expected

    def test_plan_stores_engineering_refs(self, project_id):
        refs = {"source_code": {"files": []}}
        plan = build_security_plan(project_id, "f", refs)
        assert plan.engineering_refs == refs

    def test_plan_has_unique_plan_id(self, project_id):
        p1 = build_security_plan(project_id, "f", {})
        p2 = build_security_plan(project_id, "f", {})
        assert p1.plan_id != p2.plan_id

    def test_code_team_has_three_workers(self, project_id):
        plan = build_security_plan(project_id, "f", {})
        code_tasks = plan.tasks_by_team(ScanTeam.CODE)
        assert {t.worker_agent_id for t in code_tasks} == set(CODE_WORKERS)


class TestTopologicalBatches:
    def test_independent_tasks_in_one_batch(self):
        tasks = [SecurityTask(project_id="p", team=ScanTeam.CODE, worker_agent_id=f"w{i}") for i in range(3)]
        batches = topological_batches(tasks)
        assert len(batches) == 1 and len(batches[0]) == 3

    def test_dependent_tasks_split_into_batches(self):
        t1 = SecurityTask(project_id="p", team=ScanTeam.CODE, worker_agent_id="w1")
        t2 = SecurityTask(project_id="p", team=ScanTeam.CODE, worker_agent_id="w2", depends_on=[t1.task_id])
        batches = topological_batches([t1, t2])
        assert len(batches) == 2
        assert batches[0] == [t1] and batches[1] == [t2]

    def test_cycle_raises_value_error(self):
        t1 = SecurityTask(project_id="p", team=ScanTeam.CODE, worker_agent_id="w1")
        t2 = SecurityTask(project_id="p", team=ScanTeam.CODE, worker_agent_id="w2", depends_on=[t1.task_id])
        t1.depends_on = [t2.task_id]
        with pytest.raises(ValueError):
            topological_batches([t1, t2])

    def test_real_plan_batches_correctly(self, project_id):
        plan = build_security_plan(project_id, "f", {})
        batches = topological_batches(plan.tasks)
        assert sum(len(b) for b in batches) == len(plan.tasks)


class TestTeamProgress:
    def test_progress_counts(self, project_id):
        plan = build_security_plan(project_id, "f", {})
        plan.tasks[0].status = SecurityTaskStatus.COMPLETED
        progress = team_progress(plan, plan.tasks[0].team)
        assert progress["completed"] >= 1
        assert progress["total"] >= 1


class TestClassifyFindings:
    def _clean_reports(self, project_id):
        return dict(
            dependency_scan=DependencyScan(project_id=project_id),
            static_analysis=StaticAnalysisReport(project_id=project_id),
            secret_scan=SecretScan(project_id=project_id),
            license_report=LicenseReport(project_id=project_id),
            compliance_report=ComplianceReport(project_id=project_id),
        )

    def test_no_findings_when_everything_clean(self, project_id):
        findings = classify_findings(project_id, **self._clean_reports(project_id))
        assert findings == []

    def test_critical_cve_produces_finding(self, project_id):
        reports = self._clean_reports(project_id)
        reports["dependency_scan"] = DependencyScan(project_id=project_id, vulnerabilities=[
            Vulnerability(package="django", cve_id="CVE-1", severity=FindingSeverity.CRITICAL)])
        findings = classify_findings(project_id, **reports)
        assert any(f.category == FindingCategory.CVE and f.severity == FindingSeverity.CRITICAL for f in findings)

    def test_owasp_finding_produces_finding(self, project_id):
        reports = self._clean_reports(project_id)
        reports["static_analysis"] = StaticAnalysisReport(project_id=project_id, owasp_findings=[
            CodeIssue(rule="broken_auth", file="app/auth.py", severity=FindingSeverity.HIGH)])
        findings = classify_findings(project_id, **reports)
        assert any(f.category == FindingCategory.OWASP for f in findings)

    def test_injection_finding_produces_finding(self, project_id):
        reports = self._clean_reports(project_id)
        reports["static_analysis"] = StaticAnalysisReport(project_id=project_id, injection_findings=[
            CodeIssue(rule="sql_injection", file="app/db.py", severity=FindingSeverity.CRITICAL)])
        findings = classify_findings(project_id, **reports)
        assert any(f.category == FindingCategory.INJECTION and f.severity == FindingSeverity.CRITICAL
                   for f in findings)

    def test_secret_produces_critical_finding(self, project_id):
        reports = self._clean_reports(project_id)
        reports["secret_scan"] = SecretScan(project_id=project_id, secrets=[
            SecretHit(file="a.py", rule="aws_access_key", line=1)])
        findings = classify_findings(project_id, **reports)
        secret_findings = [f for f in findings if f.category == FindingCategory.SECRET]
        assert secret_findings and secret_findings[0].severity == FindingSeverity.CRITICAL

    def test_disallowed_license_produces_finding(self, project_id):
        reports = self._clean_reports(project_id)
        reports["license_report"] = LicenseReport(project_id=project_id, disallowed_licenses=["GPL-3.0"])
        findings = classify_findings(project_id, **reports)
        assert any(f.category == FindingCategory.LICENSE for f in findings)

    def test_compliance_violation_produces_finding(self, project_id):
        reports = self._clean_reports(project_id)
        reports["compliance_report"] = ComplianceReport(project_id=project_id, violations=["missing_review"])
        findings = classify_findings(project_id, **reports)
        assert any(f.category == FindingCategory.COMPLIANCE_VIOLATION for f in findings)

    def test_multiple_issues_produce_multiple_findings(self, project_id):
        reports = self._clean_reports(project_id)
        reports["secret_scan"] = SecretScan(project_id=project_id, secrets=[
            SecretHit(file="a.py", rule="aws_access_key", line=1)])
        reports["license_report"] = LicenseReport(project_id=project_id, disallowed_licenses=["GPL-3.0"])
        findings = classify_findings(project_id, **reports)
        categories = {f.category for f in findings}
        assert FindingCategory.SECRET in categories
        assert FindingCategory.LICENSE in categories

    def test_finding_ids_deterministic_across_calls(self, project_id):
        reports = self._clean_reports(project_id)
        reports["license_report"] = LicenseReport(project_id=project_id, disallowed_licenses=["GPL-3.0"])
        f1 = classify_findings(project_id, **reports)
        f2 = classify_findings(project_id, **reports)
        assert f1[0].finding_id == f2[0].finding_id


class TestBuildRiskAssessment:
    def test_zero_findings_gives_low_risk(self, project_id):
        risk = build_risk_assessment(project_id, [])
        assert risk.risk_level == "low" and risk.risk_score == 0.0

    def test_single_critical_gives_medium_or_higher(self, project_id):
        findings = [SecurityFinding(project_id=project_id, category=FindingCategory.SECRET,
                                     severity=FindingSeverity.CRITICAL, description="x", source_worker="w")]
        risk = build_risk_assessment(project_id, findings)
        assert risk.risk_score == 40.0
        assert risk.risk_level == "medium"

    def test_two_critical_gives_high_risk(self, project_id):
        findings = [SecurityFinding(project_id=project_id, category=FindingCategory.SECRET,
                                     severity=FindingSeverity.CRITICAL, description="x", source_worker="w")
                    for _ in range(2)]
        risk = build_risk_assessment(project_id, findings)
        assert risk.risk_score == 80.0
        assert risk.risk_level == "critical"

    def test_score_caps_at_100(self, project_id):
        findings = [SecurityFinding(project_id=project_id, category=FindingCategory.SECRET,
                                     severity=FindingSeverity.CRITICAL, description="x", source_worker="w")
                    for _ in range(5)]
        risk = build_risk_assessment(project_id, findings)
        assert risk.risk_score == 100.0

    def test_contributing_factors_only_include_blocking(self, project_id):
        findings = [
            SecurityFinding(project_id=project_id, category=FindingCategory.SECRET,
                             severity=FindingSeverity.CRITICAL, description="x", source_worker="w"),
            SecurityFinding(project_id=project_id, category=FindingCategory.LICENSE,
                             severity=FindingSeverity.MEDIUM, description="y", source_worker="w"),
        ]
        risk = build_risk_assessment(project_id, findings)
        assert len(risk.contributing_factors) == 1

    def test_medium_only_findings_give_medium_or_low_risk(self, project_id):
        findings = [SecurityFinding(project_id=project_id, category=FindingCategory.LICENSE,
                                     severity=FindingSeverity.MEDIUM, description="x", source_worker="w")]
        risk = build_risk_assessment(project_id, findings)
        assert risk.risk_level in ("low", "medium")


class TestBuildSecurityReport:
    def test_verdict_pass_with_no_findings(self, project_id):
        risk = build_risk_assessment(project_id, [])
        report = build_security_report(project_id, [], risk)
        assert report.verdict == SecurityVerdict.PASS
        assert report.retry_requested is False

    def test_verdict_fail_with_blocking_finding(self, project_id):
        findings = [SecurityFinding(project_id=project_id, category=FindingCategory.SECRET,
                                     severity=FindingSeverity.CRITICAL, description="x", source_worker="w")]
        risk = build_risk_assessment(project_id, findings)
        report = build_security_report(project_id, findings, risk)
        assert report.verdict == SecurityVerdict.FAIL
        assert report.retry_requested is True

    def test_verdict_warn_with_only_nonblocking_findings(self, project_id):
        findings = [SecurityFinding(project_id=project_id, category=FindingCategory.LICENSE,
                                     severity=FindingSeverity.MEDIUM, description="x", source_worker="w")]
        risk = build_risk_assessment(project_id, findings)
        report = build_security_report(project_id, findings, risk)
        assert report.verdict == SecurityVerdict.WARN
        assert report.retry_requested is False

    def test_report_includes_risk_level_and_score(self, project_id):
        findings = [SecurityFinding(project_id=project_id, category=FindingCategory.SECRET,
                                     severity=FindingSeverity.CRITICAL, description="x", source_worker="w")]
        risk = build_risk_assessment(project_id, findings)
        report = build_security_report(project_id, findings, risk)
        assert report.risk_level == risk.risk_level
        assert report.risk_score == risk.risk_score

    def test_report_finding_ids_match_input_findings(self, project_id):
        findings = [SecurityFinding(project_id=project_id, category=FindingCategory.SECRET,
                                     severity=FindingSeverity.CRITICAL, description="x", source_worker="w")]
        risk = build_risk_assessment(project_id, findings)
        report = build_security_report(project_id, findings, risk)
        assert set(report.finding_ids) == {f.finding_id for f in findings}


class TestBuildRetryRequest:
    def test_creates_retry_request_with_reason(self, project_id):
        r = build_retry_request(project_id, "engineering", "critical secret detected")
        assert r.target_team == "engineering"
        assert r.reason == "critical secret detected"
        assert r.can_retry is True


# ══════════════════════════════════════════════════════════════
# LAYER 1d — Unit: routing predicates
# ══════════════════════════════════════════════════════════════

class TestSecurityRouting:
    def test_route_after_validate_inputs_ok(self):
        assert route_after_validate_inputs({"phase_status": "running"}) == "static_analysis"

    def test_route_after_validate_inputs_failed(self):
        assert route_after_validate_inputs({"phase_status": "failed"}) == "failed"

    def test_route_after_static_analysis_ok(self):
        assert route_after_static_analysis({"phase_status": "running"}) == "fan_out"

    def test_route_after_static_analysis_dlq(self):
        assert route_after_static_analysis({"phase_status": "running", "any_dead_lettered": True}) == "dlq"

    def test_route_after_static_analysis_failed(self):
        assert route_after_static_analysis({"phase_status": "failed"}) == "failed"

    def test_route_after_fan_out_ok(self):
        assert route_after_fan_out({"phase_status": "running"}) == "risk_classification"

    def test_route_after_fan_out_failed(self):
        assert route_after_fan_out({"phase_status": "failed"}) == "failed"

    def test_route_after_risk_classification_always_aggregate(self):
        assert route_after_risk_classification({}) == "aggregate"

    def test_route_after_aggregate_pass(self):
        assert route_after_aggregate({"verdict": "pass"}) == "publish"

    def test_route_after_aggregate_warn_still_publishes(self):
        assert route_after_aggregate({"verdict": "warn"}) == "publish"

    def test_route_after_aggregate_fail(self):
        assert route_after_aggregate({"verdict": "fail"}) == "security_findings"

    def test_route_after_security_findings_within_budget(self):
        assert route_after_security_findings({"retry_cycles_run": 1}) == "return_to_engineering"

    def test_route_after_security_findings_exhausted(self):
        assert route_after_security_findings({"retry_cycles_run": MAX_RETRY_CYCLES}) == "failed"

    def test_route_task_retry_done(self):
        assert route_task_retry({"status": "completed"}) == "done"

    def test_route_task_retry_retry(self):
        assert route_task_retry({"status": "failed", "retry_count": 1}) == "retry"

    def test_route_task_retry_dead_letter(self):
        assert route_task_retry({"status": "failed", "retry_count": MAX_TASK_RETRIES}) == "dead_letter"

    def test_route_checkpoint_recovery_valid_stage(self):
        assert route_checkpoint_recovery({"resume_at_stage": "fan_out"}) == "fan_out"

    def test_route_checkpoint_recovery_invalid_stage_defaults(self):
        assert route_checkpoint_recovery({"resume_at_stage": "bogus"}) == "validate"

    def test_route_checkpoint_recovery_missing_stage_defaults(self):
        assert route_checkpoint_recovery({}) == "validate"


# ══════════════════════════════════════════════════════════════
# LAYER 1e — Unit: agent registry
# ══════════════════════════════════════════════════════════════

class TestSecurityRegistry:
    SECURITY_AGENT_IDS = [
        "security_head", "dependency_scan_lead", "cve_scanner_worker",
        "code_security_lead", "owasp_checker_worker", "secret_scanner_worker",
        "injection_check_worker", "compliance_lead", "compliance_validator_worker",
    ]

    def test_all_security_agent_ids_registered(self):
        for agent_id in self.SECURITY_AGENT_IDS:
            assert agent_id in AGENT_REGISTRY, f"{agent_id} missing from AGENT_REGISTRY"

    def test_security_department_has_exactly_nine_agents(self):
        security_agents = [a for a in AGENT_REGISTRY.values() if a.department == "security"]
        assert len(security_agents) == 9

    def test_security_head_layer_and_parent(self):
        spec = AGENT_REGISTRY["security_head"]
        assert spec.layer == 3 and spec.role == "head" and spec.parent_agent_id == "manager_agent"

    @pytest.mark.parametrize("lead_id", ["dependency_scan_lead", "code_security_lead", "compliance_lead"])
    def test_leads_report_to_security_head(self, lead_id):
        spec = AGENT_REGISTRY[lead_id]
        assert spec.layer == 4 and spec.role == "lead" and spec.parent_agent_id == "security_head"

    def test_cve_scanner_reports_to_dependency_scan_lead(self):
        spec = AGENT_REGISTRY["cve_scanner_worker"]
        assert spec.parent_agent_id == "dependency_scan_lead" and spec.layer == 5

    def test_owasp_checker_reports_to_code_security_lead(self):
        spec = AGENT_REGISTRY["owasp_checker_worker"]
        assert spec.parent_agent_id == "code_security_lead"

    def test_secret_scanner_reports_to_code_security_lead(self):
        spec = AGENT_REGISTRY["secret_scanner_worker"]
        assert spec.parent_agent_id == "code_security_lead"

    def test_injection_check_reports_to_code_security_lead(self):
        spec = AGENT_REGISTRY["injection_check_worker"]
        assert spec.parent_agent_id == "code_security_lead"

    def test_compliance_validator_reports_to_compliance_lead(self):
        spec = AGENT_REGISTRY["compliance_validator_worker"]
        assert spec.parent_agent_id == "compliance_lead"

    def test_factory_creates_security_head_with_correct_class(self):
        from core.runtime.factory import AgentFactory
        import services.security  # noqa: F401 — ensure registration side effects ran
        factory = AgentFactory(db_factory=lambda: None, nats=None, storage=None,
                                audit_repo=None, artifact_repo=None, token_repo=None)
        agent = factory.create("security_head")
        assert agent.agent_id == "security_head" and agent.department == "security"

    def test_factory_creates_every_security_worker(self):
        from core.runtime.factory import AgentFactory
        import services.security  # noqa: F401
        factory = AgentFactory(db_factory=lambda: None, nats=None, storage=None,
                                audit_repo=None, artifact_repo=None, token_repo=None)
        for agent_id in self.SECURITY_AGENT_IDS:
            agent = factory.create(agent_id)
            assert agent.agent_id == agent_id


# ══════════════════════════════════════════════════════════════
# LAYER 2 — Graph: node functions + graph construction
# ══════════════════════════════════════════════════════════════

class TestSecurityGraph:
    def _base_state(self, project_id="p") -> Dict[str, Any]:
        return {
            "project_id": project_id, "workflow_id": "wf-1", "feature_name": "f",
            "inputs_valid": True, "static_analysis_ready": False, "secret_scan_ready": False,
            "dependency_scan_ready": False, "compliance_ready": False,
            "risk_score": 0.0, "verdict": "pass", "retry_cycles_run": 0,
            "any_dead_lettered": False, "dlq_tasks": [],
            "phase_status": "running", "failure_reason": None, "resume_at_stage": None,
            "nats_events_queue": [], "ws_events_queue": [],
        }

    def test_graph_builds_without_error(self):
        from services.security.workflows.security_graph import build_security_graph
        assert build_security_graph() is not None

    def test_graph_builds_with_checkpointer_kwarg_path(self):
        from services.security.workflows.security_graph import build_security_graph
        assert build_security_graph(checkpointer=None) is not None

    @pytest.mark.asyncio
    async def test_receive_engineering_node_starts_phase(self):
        from services.security.workflows.security_graph import receive_engineering_node
        r = await receive_engineering_node(self._base_state())
        assert r["phase_status"] == "running"

    @pytest.mark.asyncio
    async def test_validate_inputs_node_valid(self):
        from services.security.workflows.security_graph import validate_inputs_node
        r = await validate_inputs_node(self._base_state())
        assert r["phase_status"] == "running"

    @pytest.mark.asyncio
    async def test_validate_inputs_node_invalid(self):
        from services.security.workflows.security_graph import validate_inputs_node
        s = self._base_state(); s["inputs_valid"] = False
        r = await validate_inputs_node(s)
        assert r["phase_status"] == "failed" and r["failure_reason"]

    @pytest.mark.asyncio
    async def test_static_analysis_node(self):
        from services.security.workflows.security_graph import static_analysis_node
        r = await static_analysis_node(self._base_state())
        assert r["static_analysis_ready"] is True

    @pytest.mark.asyncio
    async def test_secret_scan_node(self):
        from services.security.workflows.security_graph import secret_scan_node
        r = await secret_scan_node(self._base_state())
        assert r["secret_scan_ready"] is True

    @pytest.mark.asyncio
    async def test_dependency_scan_node(self):
        from services.security.workflows.security_graph import dependency_scan_node
        r = await dependency_scan_node(self._base_state())
        assert r["dependency_scan_ready"] is True

    @pytest.mark.asyncio
    async def test_compliance_scan_node(self):
        from services.security.workflows.security_graph import compliance_scan_node
        r = await compliance_scan_node(self._base_state())
        assert r["compliance_ready"] is True

    @pytest.mark.asyncio
    async def test_risk_classification_node_publishes_event(self):
        from services.security.workflows.security_graph import risk_classification_node
        r = await risk_classification_node(self._base_state())
        subjects = [e["subject"] for e in r["nats_events_queue"]]
        assert "security.scan.completed" in subjects

    @pytest.mark.asyncio
    async def test_aggregate_results_all_ready(self):
        from services.security.workflows.security_graph import aggregate_results_node
        s = self._base_state()
        s.update(static_analysis_ready=True, secret_scan_ready=True,
                 dependency_scan_ready=True, compliance_ready=True)
        r = await aggregate_results_node(s)
        assert r["phase_status"] == "running"

    @pytest.mark.asyncio
    async def test_aggregate_results_missing_team_fails(self):
        from services.security.workflows.security_graph import aggregate_results_node
        s = self._base_state()
        s.update(static_analysis_ready=True, secret_scan_ready=False,
                 dependency_scan_ready=True, compliance_ready=True)
        r = await aggregate_results_node(s)
        assert r["phase_status"] == "failed"

    @pytest.mark.asyncio
    async def test_security_findings_node_increments_cycle(self):
        from services.security.workflows.security_graph import security_findings_node
        s = self._base_state(); s["retry_cycles_run"] = 1
        r = await security_findings_node(s)
        assert r["retry_cycles_run"] == 2

    @pytest.mark.asyncio
    async def test_return_to_engineering_node_publishes_retry_event(self):
        from services.security.workflows.security_graph import return_to_engineering_node
        r = await return_to_engineering_node(self._base_state())
        subjects = [e["subject"] for e in r["nats_events_queue"]]
        assert "security.retry.requested" in subjects

    @pytest.mark.asyncio
    async def test_dlq_node_sets_failed_with_reason(self):
        from services.security.workflows.security_graph import dlq_node
        s = self._base_state(); s["dlq_tasks"] = ["t1", "t2"]
        r = await dlq_node(s)
        assert r["phase_status"] == "failed" and "t1" in r["failure_reason"]

    @pytest.mark.asyncio
    async def test_publish_artifacts_node_completes_phase(self):
        from services.security.workflows.security_graph import publish_artifacts_node
        r = await publish_artifacts_node(self._base_state())
        subjects = [e["subject"] for e in r["nats_events_queue"]]
        assert r["phase_status"] == "completed"
        assert "security.phase.completed" in subjects

    @pytest.mark.asyncio
    async def test_handle_failure_node_publishes_failure_event(self):
        from services.security.workflows.security_graph import handle_failure_node
        s = self._base_state(); s["failure_reason"] = "boom"
        r = await handle_failure_node(s)
        subjects = [e["subject"] for e in r["nats_events_queue"]]
        assert "security.phase.failed" in subjects


# ══════════════════════════════════════════════════════════════
# LAYER 3a — Integration: read-only Repository Service client
# ══════════════════════════════════════════════════════════════

class TestSecurityRepositoryReadClient:
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
        client = SecurityRepositoryReadClient(base_url="http://test")
        mock_client = self._mock_client({"id": "repo-1"})
        with patch("services.security.integration.repository_client.httpx.AsyncClient", return_value=mock_client):
            result = await client.get_repository("p1")
        mock_client.get.assert_called_once_with("/repositories/p1")
        assert result["id"] == "repo-1"

    @pytest.mark.asyncio
    async def test_list_branches_calls_correct_path(self):
        client = SecurityRepositoryReadClient(base_url="http://test")
        mock_client = self._mock_client([{"name": "integration/f"}])
        with patch("services.security.integration.repository_client.httpx.AsyncClient", return_value=mock_client):
            result = await client.list_branches("p1")
        mock_client.get.assert_called_once_with("/branches/p1")
        assert result[0]["name"] == "integration/f"

    @pytest.mark.asyncio
    async def test_list_pull_requests_calls_correct_path(self):
        client = SecurityRepositoryReadClient(base_url="http://test")
        mock_client = self._mock_client([{"id": "pr-1"}])
        with patch("services.security.integration.repository_client.httpx.AsyncClient", return_value=mock_client):
            result = await client.list_pull_requests("p1")
        mock_client.get.assert_called_once_with("/pull-requests/p1")
        assert result[0]["id"] == "pr-1"

    @pytest.mark.asyncio
    async def test_get_release_history_calls_correct_path(self):
        client = SecurityRepositoryReadClient(base_url="http://test")
        mock_client = self._mock_client([{"event_type": "release.created"}])
        with patch("services.security.integration.repository_client.httpx.AsyncClient", return_value=mock_client):
            result = await client.get_release_history("p1")
        mock_client.get.assert_called_once_with("/releases/p1/history")
        assert result[0]["event_type"] == "release.created"

    @pytest.mark.asyncio
    async def test_get_commit_history_filters_commit_events(self):
        client = SecurityRepositoryReadClient(base_url="http://test")
        mock_client = self._mock_client([
            {"event_type": "commit.created"}, {"event_type": "release.created"},
        ])
        with patch("services.security.integration.repository_client.httpx.AsyncClient", return_value=mock_client):
            result = await client.get_commit_history("repo-1")
        assert len(result) == 1 and result[0]["event_type"] == "commit.created"

    @pytest.mark.asyncio
    async def test_error_response_raises_client_error_with_status(self):
        client = SecurityRepositoryReadClient(base_url="http://test")
        mock_client = self._mock_client({"detail": "not found"}, status=404)
        with patch("services.security.integration.repository_client.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(RepositoryServiceClientError) as exc_info:
                await client.get_repository("missing")
        assert exc_info.value.status_code == 404

    def test_default_base_url_uses_settings_port(self):
        client = SecurityRepositoryReadClient()
        assert "8006" in client._base_url

    def test_client_has_no_write_methods(self):
        write_verbs = ("create", "commit_files", "merge", "approve", "delete", "push")
        public_methods = [m for m in dir(SecurityRepositoryReadClient) if not m.startswith("_")]
        for m in public_methods:
            assert not any(v in m for v in write_verbs), f"Security client exposes write-capable method: {m}"


# ══════════════════════════════════════════════════════════════
# LAYER 3b — Integration: Security workers
# ══════════════════════════════════════════════════════════════

MOCK_OWASP_FINDINGS = json.dumps({
    "findings": [{"rule": "broken_access_control", "file": "app/auth.py",
                  "severity": "high", "description": "Missing role check"}],
    "files_scanned": 2, "quality_score": 0.88,
})
MOCK_INJECTION_FINDINGS = json.dumps({
    "findings": [{"rule": "sql_injection", "file": "app/db.py",
                  "severity": "critical", "description": "Raw string interpolation"}],
    "files_scanned": 2, "quality_score": 0.9,
})
MOCK_CRITIQUE_PASS = json.dumps({"passed": True, "score": 0.9, "blocking": [], "warnings": [], "suggestions": []})
MOCK_CRITIQUE_FAIL = json.dumps({"passed": False, "score": 0.4, "blocking": ["missing coverage"],
                                  "warnings": [], "suggestions": ["scan more files"]})


class TestCveScannerWorker:
    @pytest.mark.asyncio
    async def test_passes_with_clean_dependencies(self, security_task):
        from services.security.workers.dependency import CveScannerWorker
        infra = make_infra()
        agent = inject(CveScannerWorker, infra, "cve_scanner_worker")
        security_task.context.approved_artifacts["source_code"] = {
            "files": [{"path": "requirements.txt", "content": "flask==2.0.0\n"}]}
        with patch.object(agent, "_pre_execute", AsyncMock()), patch.object(agent, "_post_execute", AsyncMock()):
            result = await agent.execute(security_task)
        assert result.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_fails_with_known_vulnerable_package(self, security_task):
        from services.security.workers.dependency import CveScannerWorker
        infra = make_infra()
        agent = inject(CveScannerWorker, infra, "cve_scanner_worker")
        security_task.context.approved_artifacts["source_code"] = {
            "files": [{"path": "requirements.txt", "content": "django==2.0.0\n"}]}
        with patch.object(agent, "_pre_execute", AsyncMock()), patch.object(agent, "_post_execute", AsyncMock()):
            result = await agent.execute(security_task)
        assert result.status == TaskStatus.FAILED
        assert result.content["vulnerabilities"]

    @pytest.mark.asyncio
    async def test_respects_manifest_override(self, security_task):
        from services.security.workers.dependency import CveScannerWorker
        infra = make_infra()
        agent = inject(CveScannerWorker, infra, "cve_scanner_worker")
        security_task.context.approved_artifacts["__dependency_manifest_override__"] = [
            {"name": "log4j", "version": "2.0", "ecosystem": "maven"}]
        with patch.object(agent, "_pre_execute", AsyncMock()), patch.object(agent, "_post_execute", AsyncMock()):
            result = await agent.execute(security_task)
        assert result.status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_creates_dependency_scan_artifact(self, security_task):
        from services.security.workers.dependency import CveScannerWorker
        infra = make_infra()
        agent = inject(CveScannerWorker, infra, "cve_scanner_worker")
        with patch.object(agent, "_pre_execute", AsyncMock()), patch.object(agent, "_post_execute", AsyncMock()):
            result = await agent.execute(security_task)
        assert len(result.artifacts) == 1


class TestSecretScannerWorker:
    @pytest.mark.asyncio
    async def test_passes_with_no_secrets(self, security_task):
        from services.security.workers.secrets import SecretScannerWorker
        infra = make_infra()
        agent = inject(SecretScannerWorker, infra, "secret_scanner_worker")
        with patch.object(agent, "_pre_execute", AsyncMock()), patch.object(agent, "_post_execute", AsyncMock()):
            result = await agent.execute(security_task)
        assert result.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_fails_when_secret_present(self, security_task):
        from services.security.workers.secrets import SecretScannerWorker
        infra = make_infra()
        agent = inject(SecretScannerWorker, infra, "secret_scanner_worker")
        security_task.context.approved_artifacts["source_code"] = {
            "files": [{"path": "app/config.py", "content": "key = 'AKIAABCDEFGHIJKLMNOP'"}]}
        with patch.object(agent, "_pre_execute", AsyncMock()), patch.object(agent, "_post_execute", AsyncMock()):
            result = await agent.execute(security_task)
        assert result.status == TaskStatus.FAILED
        assert result.content["secrets"]

    @pytest.mark.asyncio
    async def test_scans_all_files(self, security_task):
        from services.security.workers.secrets import SecretScannerWorker
        infra = make_infra()
        agent = inject(SecretScannerWorker, infra, "secret_scanner_worker")
        with patch.object(agent, "_pre_execute", AsyncMock()), patch.object(agent, "_post_execute", AsyncMock()):
            result = await agent.execute(security_task)
        assert result.content["files_scanned"] == 2


class TestOwaspCheckerWorker:
    @pytest.mark.asyncio
    async def test_generates_findings(self, security_task):
        from services.security.workers.code_analysis import OwaspCheckerWorker
        infra = make_infra()
        agent = inject(OwaspCheckerWorker, infra, "owasp_checker_worker")
        p1, p2, p3 = patched(agent, MOCK_OWASP_FINDINGS)
        with p1, p2, p3:
            result = await agent.execute(security_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["owasp_findings"]

    @pytest.mark.asyncio
    async def test_includes_idempotent_key(self, security_task):
        from services.security.workers.code_analysis import OwaspCheckerWorker
        infra = make_infra()
        agent = inject(OwaspCheckerWorker, infra, "owasp_checker_worker")
        p1, p2, p3 = patched(agent, MOCK_OWASP_FINDINGS)
        with p1, p2, p3:
            result = await agent.execute(security_task)
        assert result.content.get("idempotent_key")


class TestInjectionCheckWorker:
    @pytest.mark.asyncio
    async def test_generates_findings(self, security_task):
        from services.security.workers.code_analysis import InjectionCheckWorker
        infra = make_infra()
        agent = inject(InjectionCheckWorker, infra, "injection_check_worker")
        p1, p2, p3 = patched(agent, MOCK_INJECTION_FINDINGS)
        with p1, p2, p3:
            result = await agent.execute(security_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["injection_findings"]


class TestComplianceValidatorWorker:
    @pytest.mark.asyncio
    async def test_passes_with_clean_manifest(self, security_task):
        from services.security.workers.compliance import ComplianceValidatorWorker
        infra = make_infra()
        agent = inject(ComplianceValidatorWorker, infra, "compliance_validator_worker")
        security_task.context.approved_artifacts["source_code"] = {
            "files": [{"path": "requirements.txt", "content": "flask==2.0.0\n"}]}
        result = await agent.execute(security_task)
        assert result.status == TaskStatus.COMPLETED
        assert len(result.artifacts) == 3

    @pytest.mark.asyncio
    async def test_fails_with_disallowed_license(self, security_task):
        from services.security.workers.compliance import ComplianceValidatorWorker
        infra = make_infra()
        agent = inject(ComplianceValidatorWorker, infra, "compliance_validator_worker")
        security_task.context.approved_artifacts["__dependency_manifest_override__"] = [
            {"name": "some-gpl-lib", "version": "1.0", "ecosystem": "pypi"}]
        result = await agent.execute(security_task)
        assert result.status == TaskStatus.FAILED
        assert "GPL-3.0" in result.content["license_report"]["disallowed_licenses"]

    @pytest.mark.asyncio
    async def test_fails_when_required_checklist_item_missing(self, security_task):
        from services.security.workers.compliance import ComplianceValidatorWorker
        infra = make_infra()
        agent = inject(ComplianceValidatorWorker, infra, "compliance_validator_worker")
        del security_task.context.approved_artifacts["security_architecture"]
        result = await agent.execute(security_task)
        assert result.status == TaskStatus.FAILED
        assert "security_architecture_reviewed" in result.content["compliance_report"]["violations"]

    @pytest.mark.asyncio
    async def test_sbom_includes_all_dependencies(self, security_task):
        from services.security.workers.compliance import ComplianceValidatorWorker
        infra = make_infra()
        agent = inject(ComplianceValidatorWorker, infra, "compliance_validator_worker")
        security_task.context.approved_artifacts["source_code"] = {
            "files": [{"path": "requirements.txt", "content": "flask==2.0.0\nrequests==2.19.0\n"}]}
        result = await agent.execute(security_task)
        assert len(result.content["sbom"]["components"]) == 2


# ══════════════════════════════════════════════════════════════
# LAYER 3c — Integration: Security leads (fake factory)
# ══════════════════════════════════════════════════════════════

class TestDependencyScanLead:
    @pytest.mark.asyncio
    async def test_success(self, security_task):
        from services.security.leads import DependencyScanLead
        infra = make_infra()
        lead = inject(DependencyScanLead, infra, "dependency_scan_lead", layer=4, role="lead")
        factory = FakeFactory({w: ok_result(w) for w in DEPENDENCY_WORKERS})
        security_task.context.approved_artifacts["__factory__"] = factory
        result = await lead.execute(security_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["scans"] == len(DEPENDENCY_WORKERS)

    @pytest.mark.asyncio
    async def test_reports_worker_failures_without_hard_escalation(self, security_task):
        from services.security.leads import DependencyScanLead
        infra = make_infra()
        lead = inject(DependencyScanLead, infra, "dependency_scan_lead", layer=4, role="lead")
        results = {"cve_scanner_worker": fail_result("cve_scanner_worker", "critical CVE found")}
        factory = FakeFactory(results)
        security_task.context.approved_artifacts["__factory__"] = factory
        result = await lead.execute(security_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["failures"]


class TestCodeSecurityLead:
    @pytest.mark.asyncio
    async def test_success(self, security_task):
        from services.security.leads import CodeSecurityLead
        infra = make_infra()
        lead = inject(CodeSecurityLead, infra, "code_security_lead", layer=4, role="lead")
        factory = FakeFactory({w: ok_result(w) for w in CODE_WORKERS})
        security_task.context.approved_artifacts["__factory__"] = factory
        result = await lead.execute(security_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["scans"] == len(CODE_WORKERS)


class TestComplianceLead:
    @pytest.mark.asyncio
    async def test_success(self, security_task):
        from services.security.leads import ComplianceLead
        infra = make_infra()
        lead = inject(ComplianceLead, infra, "compliance_lead", layer=4, role="lead")
        factory = FakeFactory({w: ok_result(w) for w in COMPLIANCE_WORKERS})
        security_task.context.approved_artifacts["__factory__"] = factory
        result = await lead.execute(security_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["scans"] == len(COMPLIANCE_WORKERS)


# ══════════════════════════════════════════════════════════════
# LAYER 4 — E2E: SecurityHead full pipeline (fake factory chain)
# ══════════════════════════════════════════════════════════════

@pytest.mark.e2e
class TestSecurityHeadPipeline:
    def _full_success_factory(self) -> FakeFactory:
        return FakeFactory({
            "dependency_scan_lead": ok_result("dependency_scan_lead", team="dependency", scans=1),
            "code_security_lead": ok_result("code_security_lead", team="code_security", scans=3),
            "compliance_lead": ok_result("compliance_lead", team="compliance", scans=1),
        })

    def _populate_passing_artifacts(self, task, project_id):
        task.context.approved_artifacts["cve_scanner_worker"] = DependencyScan(project_id=project_id).model_dump()
        task.context.approved_artifacts["owasp_checker_worker"] = {
            "owasp_findings": [], "files_scanned": 2}
        task.context.approved_artifacts["injection_check_worker"] = {
            "injection_findings": [], "files_scanned": 2}
        task.context.approved_artifacts["secret_scanner_worker"] = SecretScan(project_id=project_id).model_dump()
        task.context.approved_artifacts["compliance_validator_worker"] = {
            "license_report": LicenseReport(project_id=project_id).model_dump(),
            "compliance_report": ComplianceReport(project_id=project_id).model_dump(),
        }

    @pytest.mark.asyncio
    async def test_full_pipeline_passes(self, security_task, project_id):
        from services.security.head import SecurityHead
        infra = make_infra()
        head = inject(SecurityHead, infra, "security_head", layer=3, role="head")
        security_task.context.approved_artifacts["__factory__"] = self._full_success_factory()
        self._populate_passing_artifacts(security_task, project_id)
        result = await head.execute(security_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["verdict"] == "pass"
        assert any(e.subject == "security.phase.completed" for e in result.nats_events)

    @pytest.mark.asyncio
    async def test_pipeline_escalates_when_required_input_missing(self, security_task_missing_inputs):
        from services.security.head import SecurityHead
        infra = make_infra()
        head = inject(SecurityHead, infra, "security_head", layer=3, role="head")
        security_task_missing_inputs.context.approved_artifacts["__factory__"] = self._full_success_factory()
        result = await head.execute(security_task_missing_inputs)
        assert result.status == TaskStatus.ESCALATED

    @pytest.mark.asyncio
    async def test_pipeline_fails_with_critical_secret(self, security_task, project_id):
        from services.security.head import SecurityHead
        infra = make_infra()
        head = inject(SecurityHead, infra, "security_head", layer=3, role="head")
        security_task.context.approved_artifacts["__factory__"] = self._full_success_factory()
        self._populate_passing_artifacts(security_task, project_id)
        security_task.context.approved_artifacts["secret_scanner_worker"] = SecretScan(
            project_id=project_id, secrets=[SecretHit(file="a.py", rule="aws_access_key", line=1)]).model_dump()
        result = await head.execute(security_task)
        assert result.status == TaskStatus.FAILED
        assert result.content["verdict"] == "fail"
        assert result.content["retry_request"] is not None

    @pytest.mark.asyncio
    async def test_pipeline_fails_and_publishes_finding_events(self, security_task, project_id):
        from services.security.head import SecurityHead
        infra = make_infra()
        head = inject(SecurityHead, infra, "security_head", layer=3, role="head")
        security_task.context.approved_artifacts["__factory__"] = self._full_success_factory()
        self._populate_passing_artifacts(security_task, project_id)
        security_task.context.approved_artifacts["cve_scanner_worker"] = DependencyScan(
            project_id=project_id, vulnerabilities=[
                Vulnerability(package="django", cve_id="CVE-1", severity=FindingSeverity.CRITICAL)],
        ).model_dump()
        result = await head.execute(security_task)
        assert result.status == TaskStatus.FAILED
        assert len(result.content["findings"]) >= 1
        assert any(e.subject == "security.phase.failed" for e in result.nats_events)

    @pytest.mark.asyncio
    async def test_pipeline_publishes_websocket_completion_event(self, security_task, project_id):
        from services.security.head import SecurityHead
        infra = make_infra()
        head = inject(SecurityHead, infra, "security_head", layer=3, role="head")
        security_task.context.approved_artifacts["__factory__"] = self._full_success_factory()
        self._populate_passing_artifacts(security_task, project_id)
        result = await head.execute(security_task)
        assert any(e.event_type == "phase_completed" for e in result.ws_events)

    @pytest.mark.asyncio
    async def test_pipeline_stores_security_plan_in_context(self, security_task, project_id):
        from services.security.head import SecurityHead
        infra = make_infra()
        head = inject(SecurityHead, infra, "security_head", layer=3, role="head")
        security_task.context.approved_artifacts["__factory__"] = self._full_success_factory()
        self._populate_passing_artifacts(security_task, project_id)
        await head.execute(security_task)
        assert "__security_plan__" in security_task.context.approved_artifacts

    @pytest.mark.asyncio
    async def test_pipeline_runs_without_factory_using_placeholders(self, security_task, project_id):
        from services.security.head import SecurityHead
        infra = make_infra()
        head = inject(SecurityHead, infra, "security_head", layer=3, role="head")
        self._populate_passing_artifacts(security_task, project_id)
        result = await head.execute(security_task)
        assert result.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)

    @pytest.mark.asyncio
    async def test_pipeline_creates_security_report_artifact(self, security_task, project_id):
        from services.security.head import SecurityHead
        infra = make_infra()
        head = inject(SecurityHead, infra, "security_head", layer=3, role="head")
        security_task.context.approved_artifacts["__factory__"] = self._full_success_factory()
        self._populate_passing_artifacts(security_task, project_id)
        result = await head.execute(security_task)
        artifact_types = [a.artifact_type for a in result.artifacts]
        assert "security_report" in artifact_types
        assert "risk_assessment" in artifact_types

    @pytest.mark.asyncio
    async def test_pipeline_warn_verdict_still_completes(self, security_task, project_id):
        from services.security.head import SecurityHead
        infra = make_infra()
        head = inject(SecurityHead, infra, "security_head", layer=3, role="head")
        security_task.context.approved_artifacts["__factory__"] = self._full_success_factory()
        self._populate_passing_artifacts(security_task, project_id)
        security_task.context.approved_artifacts["compliance_validator_worker"] = {
            "license_report": LicenseReport(project_id=project_id, disallowed_licenses=[]).model_dump(),
            "compliance_report": ComplianceReport(project_id=project_id, violations=[]).model_dump(),
        }
        security_task.context.approved_artifacts["owasp_checker_worker"] = {
            "owasp_findings": [{"rule": "info_leak", "file": "a.py", "severity": "medium",
                                 "description": "verbose error page"}],
            "files_scanned": 2,
        }
        result = await head.execute(security_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content["verdict"] == "warn"

    @pytest.mark.asyncio
    async def test_pipeline_never_calls_repository_write_methods(self, security_task, project_id):
        """Security must never touch Git state — sanity check the head never
        imports or references a write-capable repository client."""
        import services.security.head as head_module
        assert not hasattr(head_module, "commit_files")
        assert not hasattr(head_module, "create_branch")
