"""
tests/foundation/test_m4_10_final_release.py
=================================
M4.10 — Final Platform Release & Launch test suite. Mirrors the layered
testing style established in M4.6-M4.9: pure models first, then each
core module, then the CLI, then the API, then dashboard integration,
then generated on-disk artifacts (docs/examples/plugins/benchmarks/
release packaging), then Final QA / regression.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.integration.release_validation import (
    benchmark_summary, compatibility_matrix, dependency_checker, documentation_generator, environment_audit,
    final_qa, release_export, release_packaging, release_validator,
)
from services.integration.release_validation.readiness_report import build_readiness_report, scan_documentation_completeness
from services.integration.release_validation.release_validation_models import (
    BenchmarkMetric, BenchmarkSummaryReport, CheckStatus, CompatibilityEntry, CompatibilityMatrix,
    DependencyIssue, DependencyReport, DocumentationCompleteness, EnvironmentAuditItem, EnvironmentAuditReport,
    ExportFormat, InstallCheckResult, InstallReport, QACheckResult, QAReport, ReadinessReport, ReleaseCheckItem,
    ReleaseManifest, ReleaseManifestEntry, ReleaseScore, Severity,
)


# ══════════════════════════════════════════════════════════════
# LAYER 1 — release_validation_models.py (pure shapes)
# ══════════════════════════════════════════════════════════════

def test_release_score_percentage_and_grade_release_candidate():
    score = ReleaseScore(score=96, max_score=100, checks=[])
    assert score.percentage == 96.0
    assert score.grade == "release_candidate"


def test_release_score_grade_near_ready():
    assert ReleaseScore(score=85, max_score=100).grade == "near_ready"


def test_release_score_grade_needs_work():
    assert ReleaseScore(score=60, max_score=100).grade == "needs_work"


def test_release_score_grade_not_ready():
    assert ReleaseScore(score=10, max_score=100).grade == "not_ready"


def test_release_score_zero_max_score_zero_percentage():
    assert ReleaseScore(score=0, max_score=0).percentage == 0.0


def test_compatibility_matrix_all_compatible_true_when_empty():
    assert CompatibilityMatrix(entries=[]).all_compatible is True


def test_compatibility_matrix_all_compatible_false_on_incompatible_entry():
    matrix = CompatibilityMatrix(entries=[
        CompatibilityEntry(component="python", required_range=">=3.11", detected_version="3.9", compatible=False),
    ])
    assert matrix.all_compatible is False


def test_compatibility_matrix_undetermined_count():
    matrix = CompatibilityMatrix(entries=[
        CompatibilityEntry(component="docker", required_range=">=24", detected_version=None, compatible=None),
        CompatibilityEntry(component="python", required_range=">=3.11", detected_version="3.12", compatible=True),
    ])
    assert matrix.undetermined_count == 1


def test_dependency_report_clean_true_with_only_warnings():
    report = DependencyReport(total_dependencies=1, issues=[
        DependencyIssue(name="a", kind="unpinned", detail="d", severity=Severity.WARNING),
    ])
    assert report.clean is True


def test_dependency_report_clean_false_with_error():
    report = DependencyReport(total_dependencies=1, issues=[
        DependencyIssue(name="a", kind="version_conflict", detail="d", severity=Severity.ERROR),
    ])
    assert report.clean is False


def test_environment_audit_overall_status_pass():
    report = EnvironmentAuditReport(items=[EnvironmentAuditItem(name="a", category="x", status=CheckStatus.PASS)])
    assert report.overall_status == CheckStatus.PASS


def test_environment_audit_overall_status_fail_dominates():
    report = EnvironmentAuditReport(items=[
        EnvironmentAuditItem(name="a", category="x", status=CheckStatus.PASS),
        EnvironmentAuditItem(name="b", category="x", status=CheckStatus.FAIL),
    ])
    assert report.overall_status == CheckStatus.FAIL


def test_environment_audit_overall_status_warn_without_fail():
    report = EnvironmentAuditReport(items=[
        EnvironmentAuditItem(name="a", category="x", status=CheckStatus.PASS),
        EnvironmentAuditItem(name="b", category="x", status=CheckStatus.WARN),
    ])
    assert report.overall_status == CheckStatus.WARN


def test_environment_audit_all_skipped_is_skipped():
    report = EnvironmentAuditReport(items=[EnvironmentAuditItem(name="a", category="x", status=CheckStatus.SKIPPED)])
    assert report.overall_status == CheckStatus.SKIPPED


def test_environment_audit_no_items_is_pass_by_vacuous_truth():
    # No FAIL, no WARN, statuses set is empty (not == {SKIPPED}) -> falls through to PASS
    assert EnvironmentAuditReport(items=[]).overall_status == CheckStatus.PASS


def test_documentation_completeness_missing_and_pct():
    doc = DocumentationCompleteness(expected_documents=["A.md", "B.md"], present_documents=["A.md"])
    assert doc.missing_documents == ["B.md"]
    assert doc.completeness_pct == 50.0


def test_documentation_completeness_no_expected_is_100pct():
    assert DocumentationCompleteness(expected_documents=[]).completeness_pct == 100.0


def test_qa_report_pass_fail_skip_counts():
    report = QAReport(checks=[
        QACheckResult(name="a", status=CheckStatus.PASS), QACheckResult(name="b", status=CheckStatus.FAIL),
        QACheckResult(name="c", status=CheckStatus.SKIPPED),
    ])
    assert report.pass_count == 1
    assert report.fail_count == 1
    assert report.skipped_count == 1
    assert report.passed is False


def test_qa_report_passed_true_with_only_pass_and_skipped():
    report = QAReport(checks=[QACheckResult(name="a", status=CheckStatus.PASS),
                               QACheckResult(name="b", status=CheckStatus.SKIPPED)])
    assert report.passed is True


def test_release_manifest_complete_true_when_all_present():
    manifest = ReleaseManifest(version="1.0.0", entries=[
        ReleaseManifestEntry(path="a", kind="changelog", present=True),
        ReleaseManifestEntry(path="b", kind="license", present=True),
    ])
    assert manifest.complete is True


def test_release_manifest_complete_false_when_missing():
    manifest = ReleaseManifest(version="1.0.0", entries=[
        ReleaseManifestEntry(path="a", kind="changelog", present=True),
        ReleaseManifestEntry(path="b", kind="license", present=False),
    ])
    assert manifest.complete is False


def test_install_report_ready_true_all_pass_or_skipped():
    report = InstallReport(checks=[InstallCheckResult(name="python", status=CheckStatus.PASS)])
    assert report.ready is True


def test_install_report_ready_false_on_warn():
    report = InstallReport(checks=[InstallCheckResult(name="docker", status=CheckStatus.WARN)])
    assert report.ready is False


def test_readiness_report_release_candidate_ready_requires_all_green():
    score = ReleaseScore(score=100, max_score=100)
    dep = DependencyReport(total_dependencies=0, issues=[])
    matrix = CompatibilityMatrix(entries=[])
    audit = EnvironmentAuditReport(items=[])
    bench = BenchmarkSummaryReport(metrics=[])
    doc = DocumentationCompleteness(expected_documents=[])
    report = ReadinessReport(version="1.0.0", release_score=score, dependency_report=dep,
                              compatibility_matrix=matrix, environment_audit=audit, benchmark_summary=bench,
                              documentation_completeness=doc)
    assert report.release_candidate_ready is True


def test_readiness_report_not_ready_when_dependency_dirty():
    score = ReleaseScore(score=100, max_score=100)
    dep = DependencyReport(total_dependencies=1, issues=[
        DependencyIssue(name="x", kind="version_conflict", detail="d", severity=Severity.ERROR)])
    matrix = CompatibilityMatrix(entries=[])
    audit = EnvironmentAuditReport(items=[])
    bench = BenchmarkSummaryReport(metrics=[])
    doc = DocumentationCompleteness(expected_documents=[])
    report = ReadinessReport(version="1.0.0", release_score=score, dependency_report=dep,
                              compatibility_matrix=matrix, environment_audit=audit, benchmark_summary=bench,
                              documentation_completeness=doc)
    assert report.release_candidate_ready is False


# ══════════════════════════════════════════════════════════════
# LAYER 2 — compatibility_matrix.py
# ══════════════════════════════════════════════════════════════

def test_check_component_compatible_true():
    entry = compatibility_matrix.check_component("python", "3.12.1", ">=3.11,<4.0")
    assert entry.compatible is True


def test_check_component_compatible_false():
    entry = compatibility_matrix.check_component("python", "3.9.0", ">=3.11,<4.0")
    assert entry.compatible is False


def test_check_component_none_detected_is_undetermined():
    entry = compatibility_matrix.check_component("nats", None)
    assert entry.compatible is None
    assert entry.detected_version is None


def test_check_component_no_declared_range_is_undetermined():
    entry = compatibility_matrix.check_component("mystery_component", "1.0.0", required_range="")
    assert entry.compatible is None


def test_build_compatibility_matrix_defaults_have_five_components():
    matrix = compatibility_matrix.build_compatibility_matrix({})
    names = {e.component for e in matrix.entries}
    assert names == set(compatibility_matrix.DEFAULT_REQUIRED_RANGES.keys())


def test_build_compatibility_matrix_with_detected_versions():
    matrix = compatibility_matrix.build_compatibility_matrix({"python": "3.12", "postgres": "15"})
    py_entry = next(e for e in matrix.entries if e.component == "python")
    assert py_entry.compatible is True


def test_build_compatibility_matrix_extra_component_surfaced():
    matrix = compatibility_matrix.build_compatibility_matrix({"rabbitmq": "3.11"})
    assert any(e.component == "rabbitmq" for e in matrix.entries)


def test_build_compatibility_matrix_custom_required_ranges_override():
    matrix = compatibility_matrix.build_compatibility_matrix({"python": "3.9"}, required_ranges={"python": ">=3.8"})
    py_entry = next(e for e in matrix.entries if e.component == "python")
    assert py_entry.compatible is True


def test_satisfies_handles_upper_bound():
    assert compatibility_matrix._satisfies("4.0.0", ">=3.11,<4.0") is False


def test_satisfies_equality_operator():
    assert compatibility_matrix._satisfies("2.9.0", "==2.9.0") is True


# ══════════════════════════════════════════════════════════════
# LAYER 3 — dependency_checker.py
# ══════════════════════════════════════════════════════════════

@pytest.fixture()
def tmp_requirements(tmp_path):
    def _write(content: str) -> str:
        path = tmp_path / "requirements.txt"
        path.write_text(content)
        return str(path)
    return _write


def test_check_dependencies_missing_file_reports_issue(tmp_path):
    report = dependency_checker.check_dependencies(str(tmp_path / "does_not_exist.txt"))
    assert report.total_dependencies == 0
    assert report.issues[0].kind == "missing"


def test_check_dependencies_clean_pinned_file(tmp_requirements):
    path = tmp_requirements("pydantic==2.5.0\nfastapi==0.110.0\n")
    report = dependency_checker.check_dependencies(path)
    assert report.total_dependencies == 2
    assert report.clean is True


def test_check_dependencies_flags_unpinned(tmp_requirements):
    path = tmp_requirements("somepackage\n")
    report = dependency_checker.check_dependencies(path)
    assert any(i.kind == "unpinned" for i in report.issues)


def test_check_dependencies_flags_version_conflict(tmp_requirements):
    path = tmp_requirements("pkg==1.0.0\npkg==2.0.0\n")
    report = dependency_checker.check_dependencies(path)
    assert any(i.kind == "version_conflict" for i in report.issues)
    assert report.clean is False


def test_check_dependencies_flags_duplicate_same_version(tmp_requirements):
    path = tmp_requirements("pkg==1.0.0\npkg==1.0.0\n")
    report = dependency_checker.check_dependencies(path)
    assert any(i.kind == "duplicate" for i in report.issues)


def test_check_dependencies_ignores_comments_and_blank_lines(tmp_requirements):
    path = tmp_requirements("# comment\n\npkg==1.0.0\n")
    report = dependency_checker.check_dependencies(path)
    assert report.total_dependencies == 1


def test_diff_against_lockfile_no_lock_versions_degrades(tmp_requirements):
    path = tmp_requirements("pkg==1.0.0\n")
    report = dependency_checker.diff_against_lockfile(path, None)
    assert report.clean is True


def test_diff_against_lockfile_flags_mismatch(tmp_requirements):
    path = tmp_requirements("pkg==1.0.0\n")
    report = dependency_checker.diff_against_lockfile(path, {"pkg": "2.0.0"})
    assert any(i.kind == "version_conflict" for i in report.issues)


# ══════════════════════════════════════════════════════════════
# LAYER 4 — environment_audit.py
# ══════════════════════════════════════════════════════════════

def test_run_audit_zero_args_is_safe_and_mostly_skipped():
    report = environment_audit.run_audit()
    assert isinstance(report, EnvironmentAuditReport)
    assert len(report.items) > 0


def test_run_audit_reshapes_m49_statuses():
    report = environment_audit.run_audit("production")
    assert all(isinstance(i.status, CheckStatus) for i in report.items)


def test_run_audit_with_probes_passes_through():
    calls = []

    def db_ping():
        calls.append("db")

    report = environment_audit.run_audit("production", db_ping=db_ping)
    assert calls == ["db"]
    db_item = next(i for i in report.items if "database" in i.name)
    assert db_item.status == CheckStatus.PASS


# ══════════════════════════════════════════════════════════════
# LAYER 5 — benchmark_summary.py
# ══════════════════════════════════════════════════════════════

def test_is_higher_better_for_throughput():
    assert benchmark_summary.is_higher_better("api_throughput") is True
    assert benchmark_summary.is_higher_better("workflow_speed") is False


def test_build_benchmark_summary_carries_all_metrics():
    report = benchmark_summary.build_benchmark_summary({"workflow_speed": 10.0, "agent_latency": 5.0})
    names = {m.name for m in report.metrics}
    assert names == {"workflow_speed", "agent_latency"}


def test_detect_regressions_latency_style_worse_when_higher():
    report = BenchmarkSummaryReport(metrics=[BenchmarkMetric(name="workflow_speed", unit="ms", value=15.0, baseline=10.0)])
    assert benchmark_summary.detect_regressions(report) == ["workflow_speed"]


def test_detect_regressions_latency_style_ok_within_threshold():
    report = BenchmarkSummaryReport(metrics=[BenchmarkMetric(name="workflow_speed", unit="ms", value=10.5, baseline=10.0)])
    assert benchmark_summary.detect_regressions(report) == []


def test_detect_regressions_throughput_style_worse_when_lower():
    report = BenchmarkSummaryReport(metrics=[
        BenchmarkMetric(name="api_throughput", unit="ops_sec", value=80.0, baseline=100.0)])
    assert benchmark_summary.detect_regressions(report) == ["api_throughput"]


def test_detect_regressions_no_baseline_never_flagged():
    report = BenchmarkSummaryReport(metrics=[BenchmarkMetric(name="workflow_speed", unit="ms", value=1000.0, baseline=None)])
    assert benchmark_summary.detect_regressions(report) == []


def test_summary_markdown_contains_metric_names():
    report = benchmark_summary.build_benchmark_summary({"agent_latency": 3.0})
    md = benchmark_summary.summary_markdown(report)
    assert "agent_latency" in md
    assert "# Benchmark Summary" in md


# ══════════════════════════════════════════════════════════════
# LAYER 6 — release_validator.py
# ══════════════════════════════════════════════════════════════

def test_compute_release_score_perfect_inputs():
    dep = DependencyReport(total_dependencies=0, issues=[])
    matrix = CompatibilityMatrix(entries=[CompatibilityEntry(component="python", required_range=">=3.11",
                                                              detected_version="3.12", compatible=True)])
    audit = EnvironmentAuditReport(items=[EnvironmentAuditItem(name="a", category="x", status=CheckStatus.PASS)])
    doc = DocumentationCompleteness(expected_documents=["A.md"], present_documents=["A.md"])
    score = release_validator.compute_release_score(dep, matrix, audit, doc, benchmark_regressions=[])
    assert score.percentage == 100.0
    assert score.grade == "release_candidate"


def test_compute_release_score_degrades_with_failures():
    dep = DependencyReport(total_dependencies=1, issues=[
        DependencyIssue(name="x", kind="version_conflict", detail="d", severity=Severity.ERROR)])
    matrix = CompatibilityMatrix(entries=[])
    audit = EnvironmentAuditReport(items=[EnvironmentAuditItem(name="a", category="x", status=CheckStatus.FAIL)])
    doc = DocumentationCompleteness(expected_documents=["A.md"], present_documents=[])
    score = release_validator.compute_release_score(dep, matrix, audit, doc)
    assert score.percentage < 50.0
    assert score.grade == "not_ready"


def test_compute_release_score_extra_checks_included():
    dep = DependencyReport(total_dependencies=0, issues=[])
    matrix = CompatibilityMatrix(entries=[])
    audit = EnvironmentAuditReport(items=[])
    doc = DocumentationCompleteness(expected_documents=[])
    extra = [ReleaseCheckItem(name="custom", category="custom", status=CheckStatus.FAIL, weight=5.0)]
    score = release_validator.compute_release_score(dep, matrix, audit, doc, extra_checks=extra)
    assert any(c.name == "custom" for c in score.checks)


def test_compute_release_score_benchmark_regression_yields_warn():
    dep = DependencyReport(total_dependencies=0, issues=[])
    matrix = CompatibilityMatrix(entries=[])
    audit = EnvironmentAuditReport(items=[])
    doc = DocumentationCompleteness(expected_documents=[])
    score = release_validator.compute_release_score(dep, matrix, audit, doc, benchmark_regressions=["workflow_speed"])
    bench_check = next(c for c in score.checks if c.name == "benchmark_summary")
    assert bench_check.status == CheckStatus.WARN


# ══════════════════════════════════════════════════════════════
# LAYER 7 — readiness_report.py (orchestrator)
# ══════════════════════════════════════════════════════════════

def test_scan_documentation_completeness_missing_dir():
    result = scan_documentation_completeness("/nonexistent/dir/path")
    assert result.present_documents == []
    assert result.completeness_pct == 0.0


def test_scan_documentation_completeness_finds_present_docs(tmp_path):
    docs_dir = tmp_path / "generated"
    docs_dir.mkdir()
    (docs_dir / "Architecture.md").write_text("content")
    result = scan_documentation_completeness(str(docs_dir), expected=["Architecture.md", "API.md"])
    assert result.present_documents == ["Architecture.md"]
    assert result.missing_documents == ["API.md"]


def test_scan_documentation_completeness_ignores_empty_file(tmp_path):
    docs_dir = tmp_path / "generated"
    docs_dir.mkdir()
    (docs_dir / "Architecture.md").write_text("")
    result = scan_documentation_completeness(str(docs_dir), expected=["Architecture.md"])
    assert result.present_documents == []


def test_build_readiness_report_zero_args_safe():
    report = build_readiness_report("1.0.0-rc1")
    assert isinstance(report, ReadinessReport)
    assert report.version == "1.0.0-rc1"
    assert report.release_score.max_score > 0


def test_build_readiness_report_with_all_docs_present(tmp_path):
    docs_dir = tmp_path / "docs" / "generated"
    docs_dir.mkdir(parents=True)
    for name in documentation_generator.GENERATED_DOCS:
        (docs_dir / name).write_text("content")
    report = build_readiness_report("1.0.0-rc1", root=str(tmp_path), docs_generated_dir="docs/generated")
    assert report.documentation_completeness.completeness_pct == 100.0


def test_build_readiness_report_benchmark_regression_detected():
    report = build_readiness_report("1.0.0-rc1", benchmark_results={"workflow_speed": 20.0},
                                     benchmark_baseline={"workflow_speed": 10.0})
    bench_check = next(c for c in report.release_score.checks if c.name == "benchmark_summary")
    assert bench_check.status == CheckStatus.WARN


# ══════════════════════════════════════════════════════════════
# LAYER 8 — release_export.py
# ══════════════════════════════════════════════════════════════

def test_export_json_roundtrips():
    score = ReleaseScore(score=90, max_score=100)
    content = release_export.export_report(score, ExportFormat.JSON)
    parsed = json.loads(content)
    assert parsed["score"] == 90


def test_export_unknown_format_raises():
    score = ReleaseScore(score=90, max_score=100)
    with pytest.raises(release_export.ExportError):
        release_export.export_report(score, "yaml")


def test_export_markdown_produces_string():
    score = ReleaseScore(score=90, max_score=100)
    content = release_export.export_report(score, "markdown")
    assert isinstance(content, str) and len(content) > 0


def test_export_html_produces_string():
    score = ReleaseScore(score=90, max_score=100)
    content = release_export.export_report(score, "html")
    assert isinstance(content, str) and len(content) > 0


# ══════════════════════════════════════════════════════════════
# LAYER 9 — documentation_generator.py
# ══════════════════════════════════════════════════════════════

def test_generate_all_writes_ten_files(tmp_path):
    written = documentation_generator.generate_all(root=".", out_dir=str(tmp_path / "generated"))
    assert len(written) == 10
    for path in written:
        assert os.path.isfile(path)
        assert os.path.getsize(path) > 0


def test_generate_all_files_match_expected_names(tmp_path):
    written = documentation_generator.generate_all(root=".", out_dir=str(tmp_path / "generated"))
    names = {os.path.basename(p) for p in written}
    assert names == set(documentation_generator.GENERATED_DOCS)


def test_generate_architecture_lists_packages():
    content = documentation_generator.generate_architecture(root=".")
    assert "# Architecture" in content
    assert "release_validation" in content


def test_generate_database_lists_migrations():
    content = documentation_generator.generate_database(root=".")
    assert "# Database" in content
    assert "Migrations" in content


def test_generate_plugins_uses_plugin_sdk_docstring():
    content = documentation_generator.generate_plugins(root=".")
    assert "# Plugins" in content


def test_generate_all_missing_root_degrades_gracefully(tmp_path):
    written = documentation_generator.generate_all(root=str(tmp_path / "nonexistent_root"),
                                                     out_dir=str(tmp_path / "generated2"))
    assert len(written) == 10  # still writes all ten, with "not found" bodies


# ══════════════════════════════════════════════════════════════
# LAYER 10 — final_qa.py
# ══════════════════════════════════════════════════════════════

def test_run_final_qa_runs_all_seven_checks():
    report = final_qa.run_final_qa()
    names = {c.name for c in report.checks}
    assert names == {"workflow_validation", "lint", "replay", "chaos", "security", "production", "plugins"}


def test_run_final_qa_records_duration():
    report = final_qa.run_final_qa()
    assert all(c.duration_ms is not None and c.duration_ms >= 0 for c in report.checks)


def test_run_final_qa_missing_modules_are_skipped_not_failed():
    report = final_qa.run_final_qa()
    replay_check = next(c for c in report.checks if c.name == "replay")
    assert replay_check.status in (CheckStatus.SKIPPED, CheckStatus.PASS)


def test_run_final_qa_custom_check_list():
    report = final_qa.run_final_qa(checks=[final_qa.check_plugins])
    assert len(report.checks) == 1
    assert report.checks[0].name == "plugins"


def test_qa_report_markdown_contains_summary_counts():
    report = final_qa.run_final_qa(checks=[final_qa.check_plugins])
    md = final_qa.qa_report_markdown(report)
    assert "QA_REPORT" in md
    assert "passed" in md


# ══════════════════════════════════════════════════════════════
# LAYER 11 — release_packaging.py
# ══════════════════════════════════════════════════════════════

def test_build_release_manifest_all_present_in_repo_root():
    manifest = release_packaging.build_release_manifest("1.0.0-rc1", root=".")
    changelog_entry = next(e for e in manifest.entries if e.kind == "changelog")
    assert changelog_entry.present is True
    assert changelog_entry.checksum is not None


def test_build_release_manifest_missing_files_in_empty_dir(tmp_path):
    manifest = release_packaging.build_release_manifest("1.0.0-rc1", root=str(tmp_path))
    assert manifest.complete is False
    assert all(e.checksum is None for e in manifest.entries if e.kind == "changelog")


def test_write_release_manifest_produces_valid_json(tmp_path):
    shutil.copytree(".", tmp_path / "repo", dirs_exist_ok=True,
                     ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
    out_path = release_packaging.write_release_manifest("1.0.0-rc1", root=str(tmp_path / "repo"))
    with open(out_path) as f:
        data = json.load(f)
    assert data["version"] == "1.0.0-rc1"


def test_checksum_is_stable_across_calls():
    c1 = release_packaging._checksum("release/CHANGELOG.md")
    c2 = release_packaging._checksum("release/CHANGELOG.md")
    assert c1 == c2 and c1 is not None


def test_checksum_none_for_missing_file():
    assert release_packaging._checksum("release/DOES_NOT_EXIST") is None


# ══════════════════════════════════════════════════════════════
# LAYER 12 — scripts/install/verify.py, doctor.py
# ══════════════════════════════════════════════════════════════

from scripts.install import doctor as install_doctor
from scripts.install import verify as install_verify


def test_check_python_passes_on_this_interpreter():
    result = install_verify.check_python()
    assert result.status == CheckStatus.PASS


def test_check_python_fails_below_required(monkeypatch):
    monkeypatch.setattr(install_verify.sys, "version_info", (3, 8, 0, "final", 0))
    result = install_verify.check_python()
    assert result.status == CheckStatus.FAIL
    assert result.fix_suggestion is not None


def test_check_dependencies_missing_file_warns(tmp_path):
    result = install_verify.check_dependencies(str(tmp_path / "nope.txt"))
    assert result.status == CheckStatus.WARN


def test_check_dependencies_all_importable(tmp_path):
    path = tmp_path / "requirements.txt"
    path.write_text("pydantic\n")
    result = install_verify.check_dependencies(str(path))
    assert result.status == CheckStatus.PASS


def test_check_dependencies_missing_package_fails(tmp_path):
    path = tmp_path / "requirements.txt"
    path.write_text("definitely_not_a_real_package_xyz\n")
    result = install_verify.check_dependencies(str(path))
    assert result.status == CheckStatus.FAIL
    assert "pip install" in result.fix_suggestion


def test_check_docker_warns_when_binary_missing(monkeypatch):
    monkeypatch.setattr(install_verify.shutil, "which", lambda name: None)
    result = install_verify.check_docker()
    assert result.status == CheckStatus.WARN
    assert result.fix_suggestion is not None


def test_tcp_check_warns_on_unreachable_port():
    result = install_verify._tcp_check("redis", "localhost", 1, "install redis")
    assert result.status == CheckStatus.WARN


def test_run_all_checks_returns_six_checks():
    report = install_verify.run_all_checks()
    assert len(report.checks) == 6
    assert {c.name for c in report.checks} == {"python", "dependencies", "docker", "redis", "postgres", "nats"}


def test_doctor_diagnose_returns_zero_when_only_warnings(monkeypatch):
    def fake_report():
        return InstallReport(checks=[InstallCheckResult(name="python", status=CheckStatus.PASS),
                                      InstallCheckResult(name="docker", status=CheckStatus.WARN,
                                                          fix_suggestion="install docker")])
    monkeypatch.setattr(install_doctor, "run_all_checks", fake_report)
    assert install_doctor.diagnose() == 0


def test_doctor_diagnose_returns_one_on_hard_failure(monkeypatch):
    def fake_report():
        return InstallReport(checks=[InstallCheckResult(name="python", status=CheckStatus.FAIL,
                                                          fix_suggestion="upgrade python")])
    monkeypatch.setattr(install_doctor, "run_all_checks", fake_report)
    assert install_doctor.diagnose() == 1


def test_doctor_diagnose_returns_zero_when_all_healthy(monkeypatch):
    def fake_report():
        return InstallReport(checks=[InstallCheckResult(name="python", status=CheckStatus.PASS)])
    monkeypatch.setattr(install_doctor, "run_all_checks", fake_report)
    assert install_doctor.diagnose() == 0


# ══════════════════════════════════════════════════════════════
# LAYER 13 — benchmarks/runner.py
# ══════════════════════════════════════════════════════════════

from benchmarks import runner as benchmark_runner


def test_run_benchmarks_returns_report_with_some_metrics():
    report = benchmark_runner.run_benchmarks()
    assert isinstance(report, BenchmarkSummaryReport)
    assert len(report.metrics) >= 1  # dashboard_refresh/api_throughput always measurable in this slice


def test_measure_dashboard_refresh_returns_float_or_none():
    value = benchmark_runner.measure_dashboard_refresh()
    assert value is None or isinstance(value, float)


def test_measure_api_throughput_returns_positive_or_none():
    value = benchmark_runner.measure_api_throughput()
    assert value is None or value > 0


def test_measure_agent_latency_not_measurable_in_this_slice():
    assert benchmark_runner.measure_agent_latency() is None


def test_write_benchmark_outputs_writes_json_and_markdown(tmp_path):
    paths = benchmark_runner.write_benchmark_outputs(out_dir=str(tmp_path))
    assert os.path.isfile(paths["json"])
    assert os.path.isfile(paths["markdown"])
    with open(paths["json"]) as f:
        data = json.load(f)
    assert "metrics" in data


# ══════════════════════════════════════════════════════════════
# LAYER 14 — release_cli.py
# ══════════════════════════════════════════════════════════════

from services.integration.release_validation import release_cli


def test_cli_check_command_exit_code(capsys):
    exit_code = release_cli.main(["check", "1.0.0-rc1", "--format", "json"])
    captured = capsys.readouterr()
    assert exit_code in (0, 1)
    parsed = json.loads(captured.out)
    assert parsed["version"] == "1.0.0-rc1"


def test_cli_docs_command_writes_files(tmp_path, capsys):
    exit_code = release_cli.main(["docs", "--out", str(tmp_path / "generated")])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "wrote" in captured.out


def test_cli_install_check_command(capsys):
    exit_code = release_cli.main(["install-check"])
    assert exit_code in (0, 1)
    captured = capsys.readouterr()
    assert "python" in captured.out


def test_cli_qa_command(capsys):
    exit_code = release_cli.main(["qa"])
    assert exit_code in (0, 1)
    captured = capsys.readouterr()
    assert "workflow_validation" in captured.out


def test_cli_package_command(capsys):
    exit_code = release_cli.main(["package", "1.0.0-rc1"])
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["version"] == "1.0.0-rc1"
    assert exit_code in (0, 1)


def test_cli_benchmark_command(capsys):
    exit_code = release_cli.main(["benchmark"])
    assert exit_code == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert "metrics" in parsed


def test_cli_requires_subcommand():
    with pytest.raises(SystemExit):
        release_cli.main([])


# ══════════════════════════════════════════════════════════════
# LAYER 15 — services/integration/api/release.py
# ══════════════════════════════════════════════════════════════

from services.integration.api.release import router as release_router


@pytest.fixture()
def api_client():
    app = FastAPI()
    app.include_router(release_router)
    return TestClient(app)


def test_api_status_empty_before_check(api_client):
    resp = api_client.get("/platform/release/status")
    assert resp.status_code == 200
    assert resp.json()["available"] is False


def test_api_check_then_status(api_client):
    resp = api_client.post("/platform/release/check", json={"version": "1.0.0-rc1"})
    assert resp.status_code == 200
    assert resp.json()["version"] == "1.0.0-rc1"

    resp2 = api_client.get("/platform/release/status")
    assert resp2.json()["available"] is True
    assert resp2.json()["report"]["version"] == "1.0.0-rc1"


def test_api_check_defaults_environment(api_client):
    resp = api_client.post("/platform/release/check", json={"version": "2.0.0"})
    assert resp.status_code == 200


def test_api_docs_generate(api_client, tmp_path):
    resp = api_client.post("/platform/release/docs/generate", json={"out_dir": str(tmp_path / "gen")})
    assert resp.status_code == 200
    assert resp.json()["count"] == 10


def test_api_docs_scan(api_client):
    resp = api_client.get("/platform/release/docs")
    assert resp.status_code == 200
    assert "expected_documents" in resp.json()


def test_api_install_check(api_client):
    resp = api_client.get("/platform/release/install-check")
    assert resp.status_code == 200
    assert "checks" in resp.json()


def test_api_benchmark_post_then_get(api_client):
    resp = api_client.post("/platform/release/benchmark")
    assert resp.status_code == 200
    resp2 = api_client.get("/platform/release/benchmark")
    assert resp2.json()["available"] is True


def test_api_benchmark_get_empty_before_run(api_client):
    resp = api_client.get("/platform/release/benchmark")
    assert resp.json()["available"] is False


def test_api_qa_post_then_get(api_client):
    resp = api_client.post("/platform/release/qa")
    assert resp.status_code == 200
    resp2 = api_client.get("/platform/release/qa")
    assert resp2.json()["available"] is True


def test_api_package_post_then_get(api_client):
    resp = api_client.post("/platform/release/package", json={"version": "1.0.0-rc1"})
    assert resp.status_code == 200
    resp2 = api_client.get("/platform/release/manifest")
    assert resp2.json()["manifest"]["version"] == "1.0.0-rc1"


def test_api_manifest_empty_before_package(api_client):
    resp = api_client.get("/platform/release/manifest")
    assert resp.json()["available"] is False


def test_api_export_requires_prior_check(api_client):
    resp = api_client.get("/platform/release/export?fmt=json")
    assert resp.status_code == 404


def test_api_export_json_after_check(api_client):
    api_client.post("/platform/release/check", json={"version": "1.0.0-rc1"})
    resp = api_client.get("/platform/release/export?fmt=json")
    assert resp.status_code == 200
    assert resp.json()["format"] == "json"


def test_api_export_bad_format_400(api_client):
    api_client.post("/platform/release/check", json={"version": "1.0.0-rc1"})
    resp = api_client.get("/platform/release/export?fmt=yaml")
    assert resp.status_code == 400


# ══════════════════════════════════════════════════════════════
# LAYER 16 — dashboard integration (dashboard_models.ReleaseSummary,
# dashboard_builder.build_release_summary / assemble_platform_dashboard)
# ══════════════════════════════════════════════════════════════

from services.integration.dashboard import dashboard_builder
from services.integration.dashboard.dashboard_models import MetricsSnapshot, ReleaseSummary


def test_build_release_summary_none_yields_none():
    assert dashboard_builder.build_release_summary(None) is None


def test_build_release_summary_populates_all_fields():
    raw = {"readiness_score": 92.5, "workflow_count": 12, "plugin_count": 4, "security_posture": 88.0,
           "chaos_score": 70.0, "production_score": "near_ready", "version": "1.0.0-rc1",
           "git_commit": "deadbeef", "build_date": "2026-07-19"}
    summary = dashboard_builder.build_release_summary(raw)
    assert isinstance(summary, ReleaseSummary)
    assert summary.readiness_score == 92.5
    assert summary.version == "1.0.0-rc1"


def test_build_release_summary_defaults_missing_keys():
    summary = dashboard_builder.build_release_summary({})
    assert summary.workflow_count == 0
    assert summary.readiness_score is None


def test_assemble_platform_dashboard_includes_release_card():
    release = ReleaseSummary(readiness_score=80.0, version="1.0.0-rc1")
    dashboard = dashboard_builder.assemble_platform_dashboard([], [], [], [], [], MetricsSnapshot(), release=release)
    assert dashboard.release is not None
    assert dashboard.release.version == "1.0.0-rc1"


def test_assemble_platform_dashboard_release_optional_and_absent_by_default():
    dashboard = dashboard_builder.assemble_platform_dashboard([], [], [], [], [], MetricsSnapshot())
    assert dashboard.release is None


def test_platform_dashboard_serializes_release_card():
    release = ReleaseSummary(readiness_score=80.0, version="1.0.0-rc1")
    dashboard = dashboard_builder.assemble_platform_dashboard([], [], [], [], [], MetricsSnapshot(), release=release)
    dumped = dashboard.model_dump(mode="json")
    assert dumped["release"]["version"] == "1.0.0-rc1"


# ══════════════════════════════════════════════════════════════
# LAYER 17 — plugins/examples/* (real Plugin SDK integration)
# ══════════════════════════════════════════════════════════════

from services.integration.plugin_sdk.plugin_loader import discover_plugins, import_entrypoint
from services.integration.plugin_sdk.plugin_models import HookType, PluginRecord
from services.integration.plugin_sdk.plugin_runtime import PluginContext, dispatch_hook

PLUGINS_EXAMPLES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                                     "plugins", "examples")


def test_discover_plugins_finds_all_three_samples():
    discovered = discover_plugins(PLUGINS_EXAMPLES_DIR)
    ids = {p.manifest.id for p in discovered}
    assert ids == {"hello_plugin", "metrics_plugin", "notification_plugin"}


def test_hello_plugin_manifest_declares_no_permissions():
    discovered = discover_plugins(PLUGINS_EXAMPLES_DIR)
    hello = next(p for p in discovered if p.manifest.id == "hello_plugin")
    assert hello.manifest.permissions == []


def test_hello_plugin_startup_hook_dispatches():
    discovered = discover_plugins(PLUGINS_EXAMPLES_DIR)
    hello = next(p for p in discovered if p.manifest.id == "hello_plugin")
    module = import_entrypoint(hello)
    record = PluginRecord(manifest=hello.manifest, source_path=hello.source_path)
    results = dispatch_hook(HookType.STARTUP, [record], {"hello_plugin": module}, PluginContext())
    assert len(results) == 1
    assert results[0].success is True
    assert "hello_plugin started" in results[0].result["message"]


def test_metrics_plugin_declares_read_permission():
    discovered = discover_plugins(PLUGINS_EXAMPLES_DIR)
    metrics = next(p for p in discovered if p.manifest.id == "metrics_plugin")
    assert "read" in metrics.manifest.permissions


def test_metrics_plugin_counters_accumulate():
    discovered = discover_plugins(PLUGINS_EXAMPLES_DIR)
    metrics = next(p for p in discovered if p.manifest.id == "metrics_plugin")
    module = import_entrypoint(metrics)
    record = PluginRecord(manifest=metrics.manifest, source_path=metrics.source_path)
    ctx = PluginContext()
    dispatch_hook(HookType.BEFORE_AGENT_RUN, [record], {"metrics_plugin": module}, ctx)
    dispatch_hook(HookType.BEFORE_AGENT_RUN, [record], {"metrics_plugin": module}, ctx)
    results = dispatch_hook(HookType.DASHBOARD_SECTION, [record], {"metrics_plugin": module}, ctx)
    assert results[0].result["agent_runs_started"] == 2


def test_notification_plugin_declares_network_and_events_permissions():
    discovered = discover_plugins(PLUGINS_EXAMPLES_DIR)
    notif = next(p for p in discovered if p.manifest.id == "notification_plugin")
    assert set(notif.manifest.permissions) == {"network", "events"}


def test_notification_plugin_incident_created_dispatches_to_sink():
    discovered = discover_plugins(PLUGINS_EXAMPLES_DIR)
    notif = next(p for p in discovered if p.manifest.id == "notification_plugin")
    module = import_entrypoint(notif)
    record = PluginRecord(manifest=notif.manifest, source_path=notif.source_path)
    sent = []
    ctx = PluginContext(config={"sink": sent.append})
    results = dispatch_hook(HookType.INCIDENT_CREATED, [record], {"notification_plugin": module}, ctx,
                             incident_id="INC-1", severity="high")
    assert results[0].success is True
    assert sent[0]["incident_id"] == "INC-1"


def test_all_sample_plugin_entrypoints_importable_without_error():
    discovered = discover_plugins(PLUGINS_EXAMPLES_DIR)
    for plugin in discovered:
        module = import_entrypoint(plugin)
        assert module is not None


# ══════════════════════════════════════════════════════════════
# LAYER 18 — examples/* (§5 "Each should be runnable")
# ══════════════════════════════════════════════════════════════

import subprocess
import sys as _sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


def _run_example(rel_path: str, extra_args=None) -> subprocess.CompletedProcess:
    return subprocess.run([_sys.executable, os.path.join(REPO_ROOT, rel_path), *(extra_args or [])],
                           capture_output=True, text=True, cwd=REPO_ROOT, timeout=30)


def test_hello_world_example_runs_successfully():
    result = _run_example("examples/hello_world/main.py")
    assert result.returncode == 0
    assert "hello_plugin started" in result.stdout


def test_bug_tracker_example_runs_successfully():
    result = _run_example("examples/bug_tracker/main.py")
    assert result.returncode == 0
    assert "critical_open_count" in result.stdout


def test_ai_chatbot_example_runs_successfully():
    result = _run_example("examples/ai_chatbot/main.py")
    assert result.returncode == 0
    assert "Goodbye" in result.stdout


def test_documentation_generator_example_runs_successfully(tmp_path):
    result = _run_example("examples/documentation_generator/main.py", ["--out", str(tmp_path / "gen")])
    assert result.returncode == 0
    assert "Generated 10 document" in result.stdout
    assert len(os.listdir(tmp_path / "gen")) == 10


def test_all_four_example_projects_present():
    expected = {"hello_world", "ai_chatbot", "bug_tracker", "documentation_generator"}
    actual = set(os.listdir(os.path.join(REPO_ROOT, "examples")))
    assert expected.issubset(actual)


# ══════════════════════════════════════════════════════════════
# LAYER 19 — release/ packaging artifacts on disk (§8)
# ══════════════════════════════════════════════════════════════

RELEASE_DIR = os.path.join(REPO_ROOT, "release")


@pytest.mark.parametrize("filename", ["CHANGELOG.md", "RELEASE_NOTES.md", "LICENSE", "NOTICE", "VERSION"])
def test_release_packaging_file_exists_and_nonempty(filename):
    path = os.path.join(RELEASE_DIR, filename)
    assert os.path.isfile(path)
    assert os.path.getsize(path) > 0


def test_version_file_matches_semver_prerelease_shape():
    with open(os.path.join(RELEASE_DIR, "VERSION")) as f:
        version = f.read().strip()
    import re
    assert re.match(r"^\d+\.\d+\.\d+(-[\w.]+)?$", version)


# ══════════════════════════════════════════════════════════════
# LAYER 20 — full-suite regression (§Success Criteria: "existing tests
# continue to pass" — a coarse in-process sanity check; the authoritative
# check is `pytest tests/foundation/` run as a whole, exercised in CI).
# ══════════════════════════════════════════════════════════════

def test_m49_production_readiness_suite_still_importable():
    import importlib
    module = importlib.import_module("tests.foundation.test_m49_production_readiness")
    assert module is not None


def test_m47_plugin_sdk_suite_still_importable():
    import importlib
    module = importlib.import_module("tests.foundation.test_m47_plugin_sdk")
    assert module is not None


def test_m48_workflow_designer_suite_still_importable():
    import importlib
    module = importlib.import_module("tests.foundation.test_m48_workflow_designer")
    assert module is not None
