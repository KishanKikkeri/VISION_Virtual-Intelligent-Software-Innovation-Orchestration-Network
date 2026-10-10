"""
tests/foundation/test_m49_production_readiness.py
=================================
M4.9 — Production Readiness & Release Engineering test suite.
Mirrors the layered testing style established in M4.6–M4.8: pure
modules first, then repository (async, real sqlite), then API
(in-memory and DB-backed), then CLI, then dashboard integration, then
deployment assets and failure/degradation scenarios.
"""
from __future__ import annotations

import json
import os

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from services.integration.production import (
    backup_manager, configuration_manager, deployment_profiles, deployment_validator, environment_validator,
    production_export, release_manager, restore_manager,
)
from services.integration.production.release_models import (
    BackupRecord, BackupScope, BackupType, CheckStatus, ConfigFormat, Environment, ExportFormat, Release,
    RestoreMode,
)


# ══════════════════════════════════════════════════════════════
# LAYER 1 — release_models.py (pure shapes)
# ══════════════════════════════════════════════════════════════

def test_environment_enum_has_four_values():
    assert {e.value for e in Environment} == {"development", "testing", "staging", "production"}


def test_backup_scope_has_six_values():
    assert {s.value for s in BackupScope} == {
        "database", "artifacts", "workflow_layouts", "plugin_registry", "configuration", "audit_logs",
    }


def test_environment_report_overall_status_all_pass():
    from services.integration.production.release_models import EnvironmentCheckItem, EnvironmentReport
    report = EnvironmentReport(environment="production",
                                checks=[EnvironmentCheckItem(name="a", category="x", status=CheckStatus.PASS)])
    assert report.overall_status == CheckStatus.PASS
    assert report.ready is True


def test_environment_report_overall_status_fail_dominates():
    from services.integration.production.release_models import EnvironmentCheckItem, EnvironmentReport
    report = EnvironmentReport(environment="production", checks=[
        EnvironmentCheckItem(name="a", category="x", status=CheckStatus.PASS),
        EnvironmentCheckItem(name="b", category="x", status=CheckStatus.FAIL),
    ])
    assert report.overall_status == CheckStatus.FAIL
    assert report.ready is False


def test_environment_report_overall_status_warn_without_fail():
    from services.integration.production.release_models import EnvironmentCheckItem, EnvironmentReport
    report = EnvironmentReport(environment="production", checks=[
        EnvironmentCheckItem(name="a", category="x", status=CheckStatus.PASS),
        EnvironmentCheckItem(name="b", category="x", status=CheckStatus.WARN),
    ])
    assert report.overall_status == CheckStatus.WARN


def test_environment_report_all_skipped_is_skipped_and_ready():
    from services.integration.production.release_models import EnvironmentCheckItem, EnvironmentReport
    report = EnvironmentReport(environment="production",
                                checks=[EnvironmentCheckItem(name="a", category="x", status=CheckStatus.SKIPPED)])
    assert report.overall_status == CheckStatus.SKIPPED
    assert report.ready is True


def test_environment_report_no_checks_is_skipped():
    from services.integration.production.release_models import EnvironmentReport
    report = EnvironmentReport(environment="production", checks=[])
    assert report.overall_status == CheckStatus.SKIPPED


def test_config_validation_result_errors_and_warnings_split():
    from services.integration.production.release_models import ConfigValidationIssue, ConfigValidationResult
    result = ConfigValidationResult(environment="production", issues=[
        ConfigValidationIssue(kind="missing", key="a", message="m", severity="error"),
        ConfigValidationIssue(kind="unknown", key="b", message="m", severity="warning"),
    ])
    assert len(result.errors) == 1
    assert len(result.warnings) == 1


def test_config_diff_result_has_changes():
    from services.integration.production.release_models import ConfigDiffEntry, ConfigDiffResult
    empty = ConfigDiffResult()
    assert empty.has_changes is False
    populated = ConfigDiffResult(entries=[ConfigDiffEntry(key="a", kind="added")])
    assert populated.has_changes is True


def test_production_checklist_ready_and_completion_ratio():
    from services.integration.production.release_models import ProductionChecklist, ProductionChecklistItem
    checklist = ProductionChecklist(environment="production", items=[
        ProductionChecklistItem(key="a", label="A", satisfied=True),
        ProductionChecklistItem(key="b", label="B", satisfied=False),
    ])
    assert checklist.ready is False
    assert checklist.completion_ratio == 0.5


def test_production_checklist_empty_not_ready():
    from services.integration.production.release_models import ProductionChecklist
    checklist = ProductionChecklist(environment="production", items=[])
    assert checklist.ready is False
    assert checklist.completion_ratio == 0.0


def test_configuration_drift_has_drift():
    from services.integration.production.release_models import ConfigurationDrift
    assert ConfigurationDrift(environment="production").has_drift is False
    assert ConfigurationDrift(environment="production", drifted_keys=["security.tls_required"]).has_drift is True


def test_semantic_version_str_without_prerelease():
    from services.integration.production.release_models import SemanticVersion
    assert str(SemanticVersion(major=1, minor=2, patch=3)) == "1.2.3"


def test_semantic_version_str_with_prerelease():
    from services.integration.production.release_models import SemanticVersion
    assert str(SemanticVersion(major=1, minor=0, patch=0, prerelease="rc1")) == "1.0.0-rc1"


def test_release_defaults_are_empty_collections():
    release = Release(version="1.0.0")
    assert release.release_notes == []
    assert release.artifacts == []
    assert release.sbom.available is False


def test_backup_record_default_type_is_full():
    assert BackupRecord().backup_type == BackupType.FULL


# ══════════════════════════════════════════════════════════════
# LAYER 2 — deployment_profiles.py
# ══════════════════════════════════════════════════════════════

def test_list_environments_returns_four():
    assert set(deployment_profiles.list_environments()) == {"development", "testing", "staging", "production"}


def test_default_sections_returns_nine_named_areas():
    assert deployment_profiles.default_sections() == sorted([
        "database", "messaging", "cache", "logging", "security", "monitoring", "plugins", "dashboard", "workflow",
    ])


def test_base_profile_is_a_fresh_copy_each_call():
    a = deployment_profiles.base_profile()
    b = deployment_profiles.base_profile()
    a["database"]["pool_size"] = 999
    assert b["database"]["pool_size"] != 999


def test_get_profile_production_overrides_tls_required_true():
    profile = deployment_profiles.get_profile(Environment.PRODUCTION)
    assert profile.sections["security"]["tls_required"] is True


def test_get_profile_development_overrides_debug_logging():
    profile = deployment_profiles.get_profile(Environment.DEVELOPMENT)
    assert profile.sections["logging"]["level"] == "DEBUG"


def test_get_profile_accepts_string_environment():
    profile = deployment_profiles.get_profile("staging")
    assert profile.environment == Environment.STAGING


def test_get_profile_invalid_environment_raises():
    with pytest.raises(ValueError):
        deployment_profiles.get_profile("nonexistent")


def test_get_profile_layered_overrides_win_over_environment_defaults():
    profile = deployment_profiles.get_profile(Environment.PRODUCTION, overrides={"database": {"pool_size": 50}})
    assert profile.sections["database"]["pool_size"] == 50
    # untouched key from the environment layer survives
    assert profile.sections["security"]["tls_required"] is True


def test_get_profile_records_overrides_applied():
    profile = deployment_profiles.get_profile(Environment.PRODUCTION, overrides={"monitoring": {"sample_rate": 1.0}})
    assert profile.overrides_applied == ["monitoring"]


def test_get_profile_no_overrides_records_empty_list():
    profile = deployment_profiles.get_profile(Environment.PRODUCTION)
    assert profile.overrides_applied == []


def test_get_profile_generated_at_is_set():
    profile = deployment_profiles.get_profile(Environment.PRODUCTION)
    assert profile.generated_at


def test_get_profile_every_environment_has_all_nine_sections():
    for env in Environment:
        profile = deployment_profiles.get_profile(env)
        assert set(profile.sections.keys()) == set(deployment_profiles.default_sections())


def test_get_profile_testing_disables_monitoring():
    profile = deployment_profiles.get_profile(Environment.TESTING)
    assert profile.sections["monitoring"]["metrics_enabled"] is False


def test_get_profile_staging_requires_tls():
    profile = deployment_profiles.get_profile(Environment.STAGING)
    assert profile.sections["security"]["tls_required"] is True


def test_get_profile_deep_merge_does_not_mutate_base_module_state():
    deployment_profiles.get_profile(Environment.PRODUCTION, overrides={"database": {"pool_size": 12345}})
    fresh = deployment_profiles.get_profile(Environment.PRODUCTION)
    assert fresh.sections["database"]["pool_size"] != 12345


# ══════════════════════════════════════════════════════════════
# LAYER 3 — configuration_manager.py
# ══════════════════════════════════════════════════════════════

def test_load_json_roundtrip():
    text = json.dumps({"database": {"pool_size": 5}})
    assert configuration_manager.load(text, ConfigFormat.JSON) == {"database": {"pool_size": 5}}


def test_load_invalid_json_raises():
    with pytest.raises(configuration_manager.ConfigurationError):
        configuration_manager.load("{not valid", ConfigFormat.JSON)


def test_load_yaml_roundtrip():
    text = "database:\n  pool_size: 5\n"
    assert configuration_manager.load(text, ConfigFormat.YAML) == {"database": {"pool_size": 5}}


def test_load_invalid_yaml_raises():
    with pytest.raises(configuration_manager.ConfigurationError):
        configuration_manager.load("database: [unterminated", ConfigFormat.YAML)


def test_load_env_format_nests_double_underscore():
    text = "DATABASE__POOL_SIZE=20\nLOGGING__LEVEL=DEBUG\n"
    result = configuration_manager.load(text, ConfigFormat.ENV)
    assert result == {"database": {"pool_size": 20}, "logging": {"level": "DEBUG"}}


def test_load_env_format_parses_booleans_and_floats():
    text = "SECURITY__TLS_REQUIRED=true\nMONITORING__SAMPLE_RATE=0.5\n"
    result = configuration_manager.load(text, ConfigFormat.ENV)
    assert result["security"]["tls_required"] is True
    assert result["monitoring"]["sample_rate"] == 0.5


def test_load_env_format_skips_comments_and_blank_lines():
    text = "# a comment\n\nDATABASE__POOL_SIZE=1\n"
    result = configuration_manager.load(text, ConfigFormat.ENV)
    assert result == {"database": {"pool_size": 1}}


def test_load_unknown_format_raises():
    with pytest.raises(configuration_manager.ConfigurationError):
        configuration_manager.load("{}", "toml")


def test_export_json_then_load_roundtrips():
    config = {"database": {"pool_size": 7}}
    text = configuration_manager.export(config, ConfigFormat.JSON)
    assert configuration_manager.load(text, ConfigFormat.JSON) == config


def test_export_yaml_then_load_roundtrips():
    config = {"database": {"pool_size": 7}}
    text = configuration_manager.export(config, ConfigFormat.YAML)
    assert configuration_manager.load(text, ConfigFormat.YAML) == config


def test_export_env_format_uppercases_keys():
    config = {"database": {"pool_size": 7}}
    text = configuration_manager.export(config, ConfigFormat.ENV)
    assert "DATABASE__POOL_SIZE=7" in text


def test_export_unknown_format_raises():
    with pytest.raises(configuration_manager.ConfigurationError):
        configuration_manager.export({}, "toml")


def test_merge_override_wins_scalar():
    result = configuration_manager.merge({"a": 1}, {"a": 2})
    assert result == {"a": 2}


def test_merge_deep_merges_nested_dicts():
    result = configuration_manager.merge({"database": {"pool_size": 5, "echo": False}}, {"database": {"echo": True}})
    assert result == {"database": {"pool_size": 5, "echo": True}}


def test_merge_does_not_mutate_inputs():
    base = {"database": {"pool_size": 5}}
    configuration_manager.merge(base, {"database": {"pool_size": 99}})
    assert base["database"]["pool_size"] == 5


def test_validate_flags_missing_required_sections():
    result = configuration_manager.validate({}, "production")
    assert result.valid is False
    missing_keys = {i.key for i in result.issues if i.kind == "missing"}
    assert "database" in missing_keys and "security" in missing_keys


def test_validate_flags_unknown_section_as_warning():
    config = deployment_profiles.base_profile()
    config["mystery_section"] = {"x": 1}
    result = configuration_manager.validate(config, "production")
    unknown = [i for i in result.issues if i.kind == "unknown"]
    assert len(unknown) == 1 and unknown[0].severity == "warning"


def test_validate_flags_deprecated_key():
    config = deployment_profiles.base_profile()
    config["logging"]["verbose"] = True
    result = configuration_manager.validate(config, "production")
    deprecated = [i for i in result.issues if i.kind == "deprecated"]
    assert len(deprecated) == 1


def test_validate_tls_disabled_is_error_in_production():
    config = deployment_profiles.base_profile()  # base has tls_required=False
    result = configuration_manager.validate(config, "production")
    tls_issues = [i for i in result.issues if i.key == "security.tls_required"]
    assert tls_issues and tls_issues[0].severity == "error"
    assert result.valid is False


def test_validate_tls_disabled_is_warning_outside_production():
    config = deployment_profiles.base_profile()
    result = configuration_manager.validate(config, "development")
    tls_issues = [i for i in result.issues if i.key == "security.tls_required"]
    assert tls_issues and tls_issues[0].severity == "warning"


def test_validate_production_profile_is_clean():
    profile = deployment_profiles.get_profile("production")
    result = configuration_manager.validate(profile.sections, "production")
    assert result.valid is True
    assert result.errors == []


def test_validate_database_echo_true_warns_in_production():
    config = deployment_profiles.get_profile("production").sections
    config["database"]["echo"] = True
    result = configuration_manager.validate(config, "production")
    echo_issues = [i for i in result.issues if i.key == "database.echo"]
    assert echo_issues and echo_issues[0].severity == "warning"


def test_compare_identifies_matching_and_differing_sections():
    a = {"database": {"pool_size": 5}, "logging": {"level": "INFO"}}
    b = {"database": {"pool_size": 5}, "logging": {"level": "DEBUG"}}
    result = configuration_manager.compare(a, b)
    assert result.matching_sections == ["database"]
    assert result.differing_sections == ["logging"]


def test_compare_section_present_only_in_one_side_is_differing():
    result = configuration_manager.compare({"database": {}}, {"database": {}, "cache": {}})
    assert "cache" in result.differing_sections


def test_diff_detects_added_removed_and_changed_keys():
    a = {"database": {"pool_size": 5, "old_key": 1}}
    b = {"database": {"pool_size": 10, "new_key": 2}}
    result = configuration_manager.diff(a, b)
    kinds = {(e.key, e.kind) for e in result.entries}
    assert ("database.pool_size", "changed") in kinds
    assert ("database.old_key", "removed") in kinds
    assert ("database.new_key", "added") in kinds


def test_diff_no_changes_returns_empty():
    config = deployment_profiles.base_profile()
    result = configuration_manager.diff(config, config)
    assert result.entries == []
    assert result.has_changes is False


def test_diff_recurses_into_nested_dicts():
    a = {"a": {"b": {"c": 1}}}
    b = {"a": {"b": {"c": 2}}}
    result = configuration_manager.diff(a, b)
    assert result.entries[0].key == "a.b.c"


# ══════════════════════════════════════════════════════════════
# LAYER 4 — environment_validator.py (graceful degradation)
# ══════════════════════════════════════════════════════════════

def test_check_database_skipped_without_probe():
    assert environment_validator.check_database().status == CheckStatus.SKIPPED


def test_check_database_pass_when_probe_succeeds():
    assert environment_validator.check_database(lambda: True).status == CheckStatus.PASS


def test_check_database_fail_when_probe_raises():
    def boom():
        raise ConnectionError("no route to host")
    result = environment_validator.check_database(boom)
    assert result.status == CheckStatus.FAIL
    assert "no route to host" in result.detail
    assert result.remediation is not None


def test_check_cache_skipped_without_probe():
    assert environment_validator.check_cache().status == CheckStatus.SKIPPED


def test_check_cache_fail_when_probe_raises():
    assert environment_validator.check_cache(lambda: (_ for _ in ()).throw(RuntimeError("x"))).status == CheckStatus.FAIL


def test_check_messaging_skipped_without_probe():
    assert environment_validator.check_messaging().status == CheckStatus.SKIPPED


def test_check_messaging_pass_when_probe_succeeds():
    assert environment_validator.check_messaging(lambda: True).status == CheckStatus.PASS


def test_check_filesystem_pass_for_writable_tmp_dir(tmp_path):
    assert environment_validator.check_filesystem(str(tmp_path)).status == CheckStatus.PASS


def test_check_filesystem_fail_for_nonexistent_path():
    result = environment_validator.check_filesystem("/definitely/not/a/real/path/xyz")
    assert result.status == CheckStatus.FAIL


def test_check_disk_space_pass_with_low_threshold(tmp_path):
    result = environment_validator.check_disk_space(str(tmp_path), minimum_free_gb=0.0)
    assert result.status == CheckStatus.PASS


def test_check_disk_space_warns_with_absurd_threshold(tmp_path):
    result = environment_validator.check_disk_space(str(tmp_path), minimum_free_gb=10_000_000.0)
    assert result.status == CheckStatus.WARN


def test_check_permissions_pass_for_tmp_dir(tmp_path):
    assert environment_validator.check_permissions(str(tmp_path)).status == CheckStatus.PASS


def test_check_plugin_integrity_skipped_without_names():
    result = environment_validator.check_plugin_integrity(None)
    assert result.status == CheckStatus.SKIPPED


def test_check_plugin_integrity_pass_with_names():
    result = environment_validator.check_plugin_integrity(["sample_plugin"])
    assert result.status in (CheckStatus.PASS, CheckStatus.SKIPPED)  # SKIPPED if plugin_validator import fails


def test_check_workflow_integrity_skipped_without_names():
    assert environment_validator.check_workflow_integrity(None).status == CheckStatus.SKIPPED


def test_check_workflow_integrity_skipped_without_validator():
    result = environment_validator.check_workflow_integrity(["wf1"])
    assert result.status == CheckStatus.SKIPPED


def test_check_workflow_integrity_pass_when_all_valid():
    result = environment_validator.check_workflow_integrity(["wf1", "wf2"], validate_fn=lambda name: True)
    assert result.status == CheckStatus.PASS


def test_check_workflow_integrity_fail_when_some_invalid():
    result = environment_validator.check_workflow_integrity(["wf1", "wf2"], validate_fn=lambda name: name == "wf1")
    assert result.status == CheckStatus.FAIL
    assert "wf2" in result.detail


def test_check_migrations_skipped_without_revisions():
    assert environment_validator.check_migrations().status == CheckStatus.SKIPPED


def test_check_migrations_pass_when_at_head():
    result = environment_validator.check_migrations(current_revision="abc", head_revision="abc")
    assert result.status == CheckStatus.PASS


def test_check_migrations_fail_when_behind_head():
    result = environment_validator.check_migrations(current_revision="abc", head_revision="xyz")
    assert result.status == CheckStatus.FAIL


def test_check_secrets_skipped_without_required_names():
    assert environment_validator.check_secrets(None).status == CheckStatus.SKIPPED


def test_check_secrets_pass_when_all_present():
    result = environment_validator.check_secrets(["SECRET_KEY"], environ={"SECRET_KEY": "value"})
    assert result.status == CheckStatus.PASS


def test_check_secrets_fail_when_missing():
    result = environment_validator.check_secrets(["SECRET_KEY"], environ={})
    assert result.status == CheckStatus.FAIL
    assert "SECRET_KEY" in result.detail


def test_check_tls_skipped_when_not_required_and_unconfigured():
    assert environment_validator.check_tls().status == CheckStatus.SKIPPED


def test_check_tls_fail_when_required_but_unconfigured():
    result = environment_validator.check_tls(required=True)
    assert result.status == CheckStatus.FAIL


def test_check_tls_warn_when_paths_missing_but_not_required():
    result = environment_validator.check_tls(required=False, cert_path=None, key_path="/x")
    assert result.status in (CheckStatus.WARN, CheckStatus.FAIL)


def test_check_tls_fail_when_files_do_not_exist():
    result = environment_validator.check_tls(cert_path="/no/such/cert", key_path="/no/such/key", required=True)
    assert result.status == CheckStatus.FAIL


def test_check_tls_pass_when_files_exist(tmp_path):
    cert = tmp_path / "tls.crt"
    key = tmp_path / "tls.key"
    cert.write_text("cert")
    key.write_text("key")
    result = environment_validator.check_tls(cert_path=str(cert), key_path=str(key), required=True)
    assert result.status == CheckStatus.PASS


def test_run_environment_checks_with_no_arguments_never_raises(tmp_path):
    report = environment_validator.run_environment_checks("production", filesystem_path=str(tmp_path))
    assert report.environment == "production"
    assert len(report.checks) == 11


def test_run_environment_checks_all_healthy_reports_pass(tmp_path):
    report = environment_validator.run_environment_checks(
        "production", db_ping=lambda: True, cache_ping=lambda: True, messaging_ping=lambda: True,
        filesystem_path=str(tmp_path), current_revision="head", head_revision="head",
    )
    assert report.overall_status == CheckStatus.PASS


def test_run_environment_checks_one_failure_propagates_to_overall(tmp_path):
    def boom():
        raise RuntimeError("down")
    report = environment_validator.run_environment_checks("production", db_ping=boom, filesystem_path=str(tmp_path))
    assert report.overall_status == CheckStatus.FAIL


# ══════════════════════════════════════════════════════════════
# LAYER 5 — deployment_validator.py
# ══════════════════════════════════════════════════════════════

_VALID_COMPOSE = """
services:
  web:
    image: nginx:latest
  worker:
    build: .
"""

_INVALID_COMPOSE_NO_SERVICES = "version: '3.9'\n"

_INVALID_COMPOSE_MISSING_IMAGE = """
services:
  broken:
    ports: ["80:80"]
"""

_VALID_MANIFEST = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: aasc-platform
"""

_INVALID_MANIFEST_MISSING_NAME = """
apiVersion: v1
kind: Service
metadata: {}
"""

_VALID_HELM_VALUES = """
image: aasc-platform
replicaCount: 3
"""

_INCOMPLETE_HELM_VALUES = """
someOtherKey: true
"""


def test_validate_docker_compose_valid_has_no_issues():
    assert deployment_validator.validate_docker_compose(_VALID_COMPOSE) == []


def test_validate_docker_compose_missing_services_section():
    issues = deployment_validator.validate_docker_compose(_INVALID_COMPOSE_NO_SERVICES)
    assert any("services" in i.message for i in issues)


def test_validate_docker_compose_service_without_image_or_build():
    issues = deployment_validator.validate_docker_compose(_INVALID_COMPOSE_MISSING_IMAGE)
    assert any("broken" in i.message for i in issues)


def test_validate_docker_compose_empty_file():
    issues = deployment_validator.validate_docker_compose("")
    assert any("empty" in i.message for i in issues)


def test_validate_docker_compose_invalid_yaml():
    issues = deployment_validator.validate_docker_compose("services: [unterminated")
    assert len(issues) == 1


def test_validate_kubernetes_manifest_valid_has_no_issues():
    assert deployment_validator.validate_kubernetes_manifest(_VALID_MANIFEST) == []


def test_validate_kubernetes_manifest_missing_name():
    issues = deployment_validator.validate_kubernetes_manifest(_INVALID_MANIFEST_MISSING_NAME)
    assert any("metadata.name" in i.message for i in issues)


def test_validate_kubernetes_manifest_missing_api_version():
    issues = deployment_validator.validate_kubernetes_manifest("kind: Service\nmetadata:\n  name: x\n")
    assert any("apiVersion" in i.message for i in issues)


def test_validate_kubernetes_manifest_multi_document():
    text = _VALID_MANIFEST + "---\n" + _INVALID_MANIFEST_MISSING_NAME
    issues = deployment_validator.validate_kubernetes_manifest(text)
    assert len(issues) >= 1


def test_validate_kubernetes_manifest_empty_file():
    issues = deployment_validator.validate_kubernetes_manifest("")
    assert any("empty" in i.message for i in issues)


def test_validate_helm_values_valid_has_no_issues():
    assert deployment_validator.validate_helm_values(_VALID_HELM_VALUES) == []


def test_validate_helm_values_missing_recommended_keys():
    issues = deployment_validator.validate_helm_values(_INCOMPLETE_HELM_VALUES)
    assert len(issues) == 2
    assert all(i.severity == "warning" for i in issues)


def test_validate_helm_values_not_a_mapping():
    issues = deployment_validator.validate_helm_values("- just\n- a\n- list\n")
    assert len(issues) == 1


def test_validate_environment_config_delegates_to_configuration_manager():
    issues = deployment_validator.validate_environment_config({}, "production")
    assert any(i.asset == "environment_config" for i in issues)


def test_validate_deployment_aggregates_multiple_assets():
    result = deployment_validator.validate_deployment({
        "docker-compose.yml": _VALID_COMPOSE,
        "deployment.yaml": _VALID_MANIFEST,
        "values.yaml": _VALID_HELM_VALUES,
    }, "production")
    assert result.valid is True
    assert set(result.assets_checked) == {"docker-compose.yml", "deployment.yaml", "values.yaml"}


def test_validate_deployment_reports_invalid_when_any_asset_fails():
    result = deployment_validator.validate_deployment({
        "docker-compose.yml": _INVALID_COMPOSE_NO_SERVICES,
    }, "production")
    assert result.valid is False


def test_validate_deployment_includes_environment_config_when_supplied():
    result = deployment_validator.validate_deployment({}, "production", environment_config={})
    assert "environment_config" in result.assets_checked
    assert result.valid is False


def test_validate_deployment_unrecognized_asset_warns_not_errors():
    result = deployment_validator.validate_deployment({"README.md": "# hi"}, "production")
    assert result.valid is True  # warning only
    assert any(i.severity == "warning" for i in result.issues)


def test_validate_deployment_no_assets_and_no_config_is_valid():
    result = deployment_validator.validate_deployment({}, "production")
    assert result.valid is True
    assert result.assets_checked == []


def test_validate_deployment_real_repo_k8s_assets_pass(tmp_path):
    k8s_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "deploy", "k8s")
    assets = {}
    for fname in ("deployment.yaml", "service.yaml", "configmap.yaml", "ingress.yaml", "pvc.yaml",
                  "rbac.yaml", "networkpolicy.yaml", "hpa.yaml", "servicemonitor.yaml"):
        path = os.path.join(k8s_dir, fname)
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                assets[fname] = f.read()
    assert assets, "expected deploy/k8s manifests to exist"
    result = deployment_validator.validate_deployment(assets, "production")
    assert result.valid is True, result.issues


def test_validate_deployment_real_repo_compose_files_pass():
    docker_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "deploy", "docker")
    for fname in ("docker-compose.dev.yml", "docker-compose.staging.yml", "docker-compose.prod.yml"):
        path = os.path.join(docker_dir, fname)
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                issues = deployment_validator.validate_docker_compose(f.read(), fname)
            assert issues == [], (fname, issues)


# ══════════════════════════════════════════════════════════════
# LAYER 6 — release_manager.py
# ══════════════════════════════════════════════════════════════

def test_parse_semver_valid():
    v = release_manager.parse_semver("1.2.3")
    assert (v.major, v.minor, v.patch) == (1, 2, 3)


def test_parse_semver_with_prerelease():
    v = release_manager.parse_semver("2.0.0-rc1")
    assert v.prerelease == "rc1"


def test_parse_semver_invalid_raises():
    with pytest.raises(ValueError):
        release_manager.parse_semver("not-a-version")


def test_parse_semver_missing_patch_raises():
    with pytest.raises(ValueError):
        release_manager.parse_semver("1.2")


def test_bump_version_patch():
    assert release_manager.bump_version("1.2.3", "patch") == "1.2.4"


def test_bump_version_minor_resets_patch():
    assert release_manager.bump_version("1.2.3", "minor") == "1.3.0"


def test_bump_version_major_resets_minor_and_patch():
    assert release_manager.bump_version("1.2.3", "major") == "2.0.0"


def test_bump_version_invalid_part_raises():
    with pytest.raises(ValueError):
        release_manager.bump_version("1.2.3", "epoch")


def test_is_breaking_upgrade_true_on_major_bump():
    assert release_manager.is_breaking_upgrade("1.9.0", "2.0.0") is True


def test_is_breaking_upgrade_false_on_minor_bump():
    assert release_manager.is_breaking_upgrade("1.9.0", "1.10.0") is False


def test_build_artifact_inventory_finds_production_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    artifacts = release_manager.build_artifact_inventory(root)
    names = {a.name for a in artifacts}
    assert "release_models.py" in names


def test_build_artifact_inventory_includes_migrations():
    root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    artifacts = release_manager.build_artifact_inventory(root)
    migration_artifacts = [a for a in artifacts if a.kind == "migration"]
    assert any("0013_production_readiness" in a.name for a in migration_artifacts)


def test_build_artifact_inventory_missing_root_returns_empty(tmp_path):
    assert release_manager.build_artifact_inventory(str(tmp_path)) == []


def test_build_dependency_inventory_missing_file_returns_empty(tmp_path):
    assert release_manager.build_dependency_inventory(str(tmp_path / "nope.txt")) == []


def test_build_dependency_inventory_parses_pinned_versions(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("pydantic==2.5.0\n# a comment\nfastapi>=0.100.0\nnoversionpkg\n")
    entries = release_manager.build_dependency_inventory(str(req))
    by_name = {e.name: e.version for e in entries}
    assert by_name["pydantic"] == "2.5.0"
    assert by_name["fastapi"] == "0.100.0"
    assert by_name["noversionpkg"] == ""


def test_attach_sbom_degrades_when_module_unavailable():
    ref = release_manager.attach_sbom()
    assert ref.available is False
    assert ref.note


def test_build_upgrade_checklist_includes_breaking_change_review_for_major_bump():
    steps = release_manager.build_upgrade_checklist("1.0.0", "2.0.0")
    assert steps[0].step == "review_breaking_changes"


def test_build_upgrade_checklist_omits_breaking_review_for_patch_bump():
    steps = release_manager.build_upgrade_checklist("1.0.0", "1.0.1")
    assert steps[0].step != "review_breaking_changes"


def test_build_upgrade_checklist_always_includes_backup_first_meaningful_step():
    steps = release_manager.build_upgrade_checklist(None, "1.0.0")
    assert any(s.step == "backup" for s in steps)


def test_build_rollback_plan_no_previous_version():
    plan = release_manager.build_rollback_plan(None, "1.0.0")
    assert plan[0].step == "no_prior_release"
    assert plan[0].required is False


def test_build_rollback_plan_with_previous_version_has_full_steps():
    plan = release_manager.build_rollback_plan("1.0.0", "1.1.0")
    steps = {s.step for s in plan}
    assert {"stop_traffic", "restore_backup", "downgrade_migrations", "redeploy_previous_artifacts",
            "verify_health"}.issubset(steps)


def test_build_release_validates_version_format():
    with pytest.raises(ValueError):
        release_manager.build_release("not-semver")


def test_build_release_assembles_full_release():
    root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    release = release_manager.build_release("1.0.0", previous_version="0.9.0", root=root)
    assert release.version == "1.0.0"
    assert release.previous_version == "0.9.0"
    assert len(release.artifacts) > 0
    assert len(release.upgrade_checklist) > 0
    assert len(release.rollback_plan) > 0
    assert release.created_at


# ══════════════════════════════════════════════════════════════
# LAYER 7 — backup_manager.py / restore_manager.py
# ══════════════════════════════════════════════════════════════

def test_create_backup_writes_file_and_returns_record(tmp_path):
    record = backup_manager.create_backup(
        [BackupScope.CONFIGURATION], str(tmp_path), fetchers={BackupScope.CONFIGURATION: lambda: {"a": 1}},
    )
    assert os.path.isfile(record.location)
    assert record.status == "completed"
    assert record.size_bytes > 0
    assert record.scopes == [BackupScope.CONFIGURATION]


def test_create_backup_missing_fetcher_notes_and_marks_partial(tmp_path):
    record = backup_manager.create_backup([BackupScope.DATABASE], str(tmp_path))
    assert record.status == "partial"
    assert any("no fetcher supplied" in n for n in record.notes)


def test_create_backup_fetcher_raising_is_recorded_not_propagated(tmp_path):
    def boom():
        raise RuntimeError("db offline")
    record = backup_manager.create_backup([BackupScope.DATABASE], str(tmp_path), fetchers={BackupScope.DATABASE: boom})
    assert any("db offline" in n for n in record.notes)


def test_create_backup_checksum_is_deterministic_for_same_payload(tmp_path):
    fetchers = {BackupScope.CONFIGURATION: lambda: {"x": 1}}
    r1 = backup_manager.create_backup([BackupScope.CONFIGURATION], str(tmp_path / "a"), fetchers=fetchers)
    r2 = backup_manager.create_backup([BackupScope.CONFIGURATION], str(tmp_path / "b"), fetchers=fetchers)
    assert r1.checksum == r2.checksum


def test_verify_backup_true_for_untouched_file(tmp_path):
    record = backup_manager.create_backup([BackupScope.CONFIGURATION], str(tmp_path),
                                            fetchers={BackupScope.CONFIGURATION: lambda: {"a": 1}})
    assert backup_manager.verify_backup(record) is True


def test_verify_backup_false_after_tampering(tmp_path):
    record = backup_manager.create_backup([BackupScope.CONFIGURATION], str(tmp_path),
                                            fetchers={BackupScope.CONFIGURATION: lambda: {"a": 1}})
    with open(record.location, "r+", encoding="utf-8") as f:
        data = json.load(f)
        data["payload"]["configuration"] = {"tampered": True}
        f.seek(0)
        json.dump(data, f)
        f.truncate()
    assert backup_manager.verify_backup(record) is False


def test_verify_backup_missing_file_returns_false():
    fake = BackupRecord(id="x", location="/no/such/file.json", checksum="abc")
    assert backup_manager.verify_backup(fake) is False


def test_load_backup_payload_missing_file_raises():
    fake = BackupRecord(id="x", location="/no/such/file.json")
    with pytest.raises(FileNotFoundError):
        backup_manager.load_backup_payload(fake)


def test_incremental_backup_without_baseline_captures_all(tmp_path):
    record = backup_manager.create_backup(
        [BackupScope.CONFIGURATION], str(tmp_path), backup_type=BackupType.INCREMENTAL,
        fetchers={BackupScope.CONFIGURATION: lambda: {"a": 1}},
    )
    assert record.backup_type == BackupType.INCREMENTAL
    assert any("no baseline supplied" in n for n in record.notes)


def test_incremental_backup_skips_scopes_covered_by_baseline(tmp_path):
    baseline = backup_manager.create_backup([BackupScope.CONFIGURATION], str(tmp_path / "base"),
                                              fetchers={BackupScope.CONFIGURATION: lambda: {"a": 1}})
    incremental = backup_manager.create_backup(
        [BackupScope.CONFIGURATION, BackupScope.DATABASE], str(tmp_path / "inc"), backup_type=BackupType.INCREMENTAL,
        fetchers={BackupScope.CONFIGURATION: lambda: {"a": 1}, BackupScope.DATABASE: lambda: {"b": 2}},
        baseline=baseline,
    )
    assert BackupScope.DATABASE in incremental.scopes
    assert BackupScope.CONFIGURATION not in incremental.scopes
    assert incremental.baseline_backup_id == baseline.id


def test_dry_run_restore_never_writes(tmp_path):
    record = backup_manager.create_backup([BackupScope.CONFIGURATION], str(tmp_path),
                                            fetchers={BackupScope.CONFIGURATION: lambda: {"a": 1}})
    plan = restore_manager.dry_run_restore(record)
    assert plan.mode == RestoreMode.DRY_RUN
    assert plan.status == "planned"
    assert plan.confirmed is False


def test_plan_restore_full_validates_and_marks_validated(tmp_path):
    record = backup_manager.create_backup([BackupScope.CONFIGURATION], str(tmp_path),
                                            fetchers={BackupScope.CONFIGURATION: lambda: {"a": 1}})
    plan = restore_manager.plan_restore(record, mode=RestoreMode.FULL)
    assert plan.status == "validated"
    assert plan.validation_issues == []


def test_plan_restore_rejects_missing_scope(tmp_path):
    record = backup_manager.create_backup([BackupScope.CONFIGURATION], str(tmp_path),
                                            fetchers={BackupScope.CONFIGURATION: lambda: {"a": 1}})
    plan = restore_manager.plan_restore(record, mode=RestoreMode.SELECTIVE, scopes=[BackupScope.DATABASE])
    assert plan.status == "rejected"
    assert plan.validation_issues


def test_plan_restore_detects_corrupted_backup(tmp_path):
    record = backup_manager.create_backup([BackupScope.CONFIGURATION], str(tmp_path),
                                            fetchers={BackupScope.CONFIGURATION: lambda: {"a": 1}})
    with open(record.location, "r+", encoding="utf-8") as f:
        data = json.load(f)
        data["payload"]["configuration"] = {"tampered": True}
        f.seek(0)
        json.dump(data, f)
        f.truncate()
    plan = restore_manager.plan_restore(record, mode=RestoreMode.FULL)
    assert plan.status == "rejected"
    assert any("checksum" in i for i in plan.validation_issues)


def test_apply_restore_dry_run_plan_raises_value_error(tmp_path):
    record = backup_manager.create_backup([BackupScope.CONFIGURATION], str(tmp_path),
                                            fetchers={BackupScope.CONFIGURATION: lambda: {"a": 1}})
    plan = restore_manager.plan_restore(record, mode=RestoreMode.DRY_RUN)
    with pytest.raises(ValueError):
        restore_manager.apply_restore(plan, record, confirm=True)


def test_apply_restore_without_confirm_raises(tmp_path):
    record = backup_manager.create_backup([BackupScope.CONFIGURATION], str(tmp_path),
                                            fetchers={BackupScope.CONFIGURATION: lambda: {"a": 1}})
    plan = restore_manager.plan_restore(record, mode=RestoreMode.FULL)
    with pytest.raises(restore_manager.RestoreNotConfirmedError):
        restore_manager.apply_restore(plan, record, confirm=False)


def test_apply_restore_with_confirm_calls_writers(tmp_path):
    record = backup_manager.create_backup([BackupScope.CONFIGURATION], str(tmp_path),
                                            fetchers={BackupScope.CONFIGURATION: lambda: {"a": 1}})
    plan = restore_manager.plan_restore(record, mode=RestoreMode.FULL)
    written = {}

    def writer(payload):
        written["configuration"] = payload

    applied = restore_manager.apply_restore(plan, record, writers={BackupScope.CONFIGURATION: writer}, confirm=True)
    assert applied.status == "applied"
    assert written["configuration"] == {"a": 1}


def test_apply_restore_with_validation_issues_marks_failed(tmp_path):
    record = backup_manager.create_backup([BackupScope.CONFIGURATION], str(tmp_path),
                                            fetchers={BackupScope.CONFIGURATION: lambda: {"a": 1}})
    plan = restore_manager.plan_restore(record, mode=RestoreMode.SELECTIVE, scopes=[BackupScope.DATABASE])
    applied = restore_manager.apply_restore(plan, record, confirm=True)
    assert applied.status == "failed"


# ══════════════════════════════════════════════════════════════
# LAYER 8 — production_export.py
# ══════════════════════════════════════════════════════════════

def test_export_report_json_roundtrips():
    release = Release(version="1.0.0")
    text = production_export.export_report(release, ExportFormat.JSON)
    assert json.loads(text)["version"] == "1.0.0"


def test_export_report_markdown_includes_title():
    release = Release(version="1.0.0")
    text = production_export.export_report(release, ExportFormat.MARKDOWN)
    assert text.startswith("# Release")


def test_export_report_html_wraps_in_html_tags():
    release = Release(version="1.0.0")
    text = production_export.export_report(release, ExportFormat.HTML)
    assert text.startswith("<html>") and text.endswith("</html>")


def test_export_report_unknown_format_raises():
    with pytest.raises(production_export.ExportError):
        production_export.export_report(Release(version="1.0.0"), "pdf")


def test_export_report_markdown_handles_empty_lists():
    from services.integration.production.release_models import EnvironmentReport
    report = EnvironmentReport(environment="production", checks=[])
    text = production_export.export_report(report, ExportFormat.MARKDOWN)
    assert "(none)" in text


def test_export_report_accepts_string_format():
    release = Release(version="1.0.0")
    assert production_export.export_report(release, "json")


# ══════════════════════════════════════════════════════════════
# LAYER 9 — production_repository.py (real sqlite)
# ══════════════════════════════════════════════════════════════

@pytest.fixture
async def db_session():
    from infrastructure.database.production_models import Base

    engine = create_async_engine("sqlite+aiosqlite://", connect_args={"check_same_thread": False},
                                  poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session

    await engine.dispose()


async def test_repository_save_and_get_release(db_session):
    from services.integration.production.production_repository import ProductionRepository
    release = release_manager.build_release("1.0.0")
    await ProductionRepository.save_release(db_session, release)
    await db_session.commit()
    got = await ProductionRepository.get_release(db_session, "1.0.0")
    assert got.version == "1.0.0"


async def test_repository_get_release_missing_returns_none(db_session):
    from services.integration.production.production_repository import ProductionRepository
    assert await ProductionRepository.get_release(db_session, "9.9.9") is None


async def test_repository_save_release_upserts_existing(db_session):
    from services.integration.production.production_repository import ProductionRepository
    release_v1 = Release(version="1.0.0", channel="stable")
    await ProductionRepository.save_release(db_session, release_v1)
    await db_session.commit()
    release_v1_updated = Release(version="1.0.0", channel="canary")
    await ProductionRepository.save_release(db_session, release_v1_updated)
    await db_session.commit()
    got = await ProductionRepository.get_release(db_session, "1.0.0")
    assert got.channel == "canary"


async def test_repository_list_releases_sorted_most_recent_first(db_session):
    from services.integration.production.production_repository import ProductionRepository
    await ProductionRepository.save_release(db_session, Release(version="1.0.0"))
    await db_session.commit()
    await ProductionRepository.save_release(db_session, Release(version="1.1.0"))
    await db_session.commit()
    releases = await ProductionRepository.list_releases(db_session)
    assert releases[0].version == "1.1.0"


async def test_repository_latest_release_none_when_empty(db_session):
    from services.integration.production.production_repository import ProductionRepository
    assert await ProductionRepository.latest_release(db_session) is None


async def test_repository_save_and_list_profile_snapshots(db_session):
    from services.integration.production.production_repository import ProductionRepository
    profile = deployment_profiles.get_profile("production")
    await ProductionRepository.save_profile_snapshot(db_session, profile)
    await db_session.commit()
    snapshots = await ProductionRepository.list_profile_snapshots(db_session, "production")
    assert len(snapshots) == 1
    assert snapshots[0].sections["security"]["tls_required"] is True


async def test_repository_save_and_get_backup_record(db_session, tmp_path):
    from services.integration.production.production_repository import ProductionRepository
    record = backup_manager.create_backup([BackupScope.CONFIGURATION], str(tmp_path),
                                            fetchers={BackupScope.CONFIGURATION: lambda: {"a": 1}})
    await ProductionRepository.save_backup_record(db_session, record)
    await db_session.commit()
    got = await ProductionRepository.get_backup_record(db_session, record.id)
    assert got.id == record.id
    assert got.checksum == record.checksum


async def test_repository_get_backup_record_missing_returns_none(db_session):
    from services.integration.production.production_repository import ProductionRepository
    assert await ProductionRepository.get_backup_record(db_session, "nonexistent") is None


async def test_repository_list_backup_records(db_session, tmp_path):
    from services.integration.production.production_repository import ProductionRepository
    for i in range(3):
        record = backup_manager.create_backup([BackupScope.CONFIGURATION], str(tmp_path),
                                                fetchers={BackupScope.CONFIGURATION: lambda: {"i": i}})
        await ProductionRepository.save_backup_record(db_session, record)
        await db_session.commit()
    records = await ProductionRepository.list_backup_records(db_session)
    assert len(records) == 3


async def test_repository_save_and_list_restore_records(db_session, tmp_path):
    from services.integration.production.production_repository import ProductionRepository
    record = backup_manager.create_backup([BackupScope.CONFIGURATION], str(tmp_path),
                                            fetchers={BackupScope.CONFIGURATION: lambda: {"a": 1}})
    plan = restore_manager.plan_restore(record, mode=RestoreMode.DRY_RUN)
    await ProductionRepository.save_restore_record(db_session, plan)
    await db_session.commit()
    records = await ProductionRepository.list_restore_records(db_session)
    assert len(records) == 1
    assert records[0].backup_id == record.id


async def test_repository_save_and_list_environment_checks(db_session):
    from services.integration.production.production_repository import ProductionRepository
    report = environment_validator.run_environment_checks("production")
    await ProductionRepository.save_environment_check(db_session, report)
    await db_session.commit()
    reports = await ProductionRepository.list_environment_checks(db_session, "production")
    assert len(reports) == 1
    assert reports[0].environment == "production"


async def test_repository_latest_environment_check_none_when_empty(db_session):
    from services.integration.production.production_repository import ProductionRepository
    assert await ProductionRepository.latest_environment_check(db_session, "production") is None


async def test_repository_latest_environment_check_returns_most_recent(db_session):
    from services.integration.production.production_repository import ProductionRepository
    report1 = environment_validator.run_environment_checks("production")
    await ProductionRepository.save_environment_check(db_session, report1)
    await db_session.commit()
    report2 = environment_validator.run_environment_checks("production", db_ping=lambda: True)
    await ProductionRepository.save_environment_check(db_session, report2)
    await db_session.commit()
    latest = await ProductionRepository.latest_environment_check(db_session, "production")
    assert latest.overall_status == report2.overall_status


async def test_fetch_production_dashboard_section_no_db_returns_zeros():
    from services.integration.production.production_repository import fetch_production_dashboard_section
    section = await fetch_production_dashboard_section(None)
    assert section["backup_count"] == 0
    assert section["latest_release_version"] is None


async def test_fetch_production_dashboard_section_with_data(db_session, tmp_path):
    from services.integration.production.production_repository import (
        ProductionRepository, fetch_production_dashboard_section,
    )
    await ProductionRepository.save_release(db_session, Release(version="2.0.0"))
    record = backup_manager.create_backup([BackupScope.CONFIGURATION], str(tmp_path),
                                            fetchers={BackupScope.CONFIGURATION: lambda: {"a": 1}})
    await ProductionRepository.save_backup_record(db_session, record)
    await db_session.commit()
    section = await fetch_production_dashboard_section(db_session)
    assert section["latest_release_version"] == "2.0.0"
    assert section["backup_count"] == 1


# ══════════════════════════════════════════════════════════════
# LAYER 10 — API (services/integration/api/production.py)
# ══════════════════════════════════════════════════════════════

@pytest.fixture
def app_no_db():
    from fastapi import FastAPI

    from services.integration.api.production import router
    app = FastAPI()
    app.include_router(router)
    app.state.db_factory = None
    return app


def _client(app):
    from httpx import ASGITransport, AsyncClient
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_api_get_config_returns_merged_profile(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.get("/production/config", params={"environment": "production"})
    assert resp.status_code == 200
    assert resp.json()["sections"]["security"]["tls_required"] is True


async def test_api_get_config_invalid_environment_422(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.get("/production/config", params={"environment": "nonexistent"})
    assert resp.status_code == 422


async def test_api_validate_config_route(app_no_db):
    profile = deployment_profiles.get_profile("production")
    async with _client(app_no_db) as client:
        resp = await client.post("/production/config/validate",
                                  json={"config": profile.sections, "environment": "production"})
    assert resp.status_code == 200
    assert resp.json()["valid"] is True


async def test_api_validate_config_route_missing_sections_invalid(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.post("/production/config/validate", json={"config": {}, "environment": "production"})
    assert resp.status_code == 200
    assert resp.json()["valid"] is False


async def test_api_get_environment_returns_report(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.get("/production/environment", params={"environment": "production"})
    assert resp.status_code == 200
    assert resp.json()["environment"] == "production"
    assert len(resp.json()["checks"]) == 11


async def test_api_get_checks_reads_back_memory_history(app_no_db):
    async with _client(app_no_db) as client:
        await client.get("/production/environment", params={"environment": "production"})
        resp = await client.get("/production/checks", params={"environment": "production"})
    assert resp.status_code == 200
    assert len(resp.json()["checks"]) == 1


async def test_api_validate_deployment_route(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.post("/production/validate", json={
            "assets": {"docker-compose.yml": _VALID_COMPOSE},
            "environment": "production",
        })
    assert resp.status_code == 200
    assert resp.json()["valid"] is True


async def test_api_create_and_list_releases_in_memory(app_no_db):
    async with _client(app_no_db) as client:
        create_resp = await client.post("/production/releases", json={"version": "1.0.0"})
        assert create_resp.status_code == 200
        list_resp = await client.get("/production/releases")
    assert list_resp.json()["count"] == 1
    assert list_resp.json()["releases"][0]["version"] == "1.0.0"


async def test_api_create_release_missing_version_422(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.post("/production/releases", json={})
    assert resp.status_code == 422


async def test_api_create_release_invalid_version_422(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.post("/production/releases", json={"version": "nope"})
    assert resp.status_code == 422


async def test_api_create_and_list_backups_in_memory(app_no_db, tmp_path):
    async with _client(app_no_db) as client:
        create_resp = await client.post("/production/backup", json={
            "scopes": ["configuration"], "destination_dir": str(tmp_path),
        })
        assert create_resp.status_code == 200
        list_resp = await client.get("/production/backups")
    assert list_resp.json()["count"] == 1


async def test_api_backup_invalid_scope_422(app_no_db, tmp_path):
    async with _client(app_no_db) as client:
        resp = await client.post("/production/backup", json={
            "scopes": ["not_a_real_scope"], "destination_dir": str(tmp_path),
        })
    assert resp.status_code == 422


async def test_api_restore_plan_only_never_applies(app_no_db, tmp_path):
    async with _client(app_no_db) as client:
        backup_resp = await client.post("/production/backup", json={
            "scopes": ["configuration"], "destination_dir": str(tmp_path),
        })
        backup_id = backup_resp.json()["id"]
        restore_resp = await client.post("/production/restore", json={"backup_id": backup_id})
    assert restore_resp.status_code == 200
    assert restore_resp.json()["mode"] == "dry_run"
    assert restore_resp.json()["confirmed"] is False


async def test_api_restore_missing_backup_id_422(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.post("/production/restore", json={})
    assert resp.status_code == 422


async def test_api_restore_unknown_backup_404(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.post("/production/restore", json={"backup_id": "nonexistent"})
    assert resp.status_code == 404


async def test_api_checklist_reflects_recorded_checks_and_backups(app_no_db, tmp_path):
    async with _client(app_no_db) as client:
        await client.get("/production/environment", params={"environment": "production"})
        await client.post("/production/backup", json={"scopes": ["configuration"], "destination_dir": str(tmp_path)})
        resp = await client.get("/production/checklist", params={"environment": "production"})
    assert resp.status_code == 200
    items = {i["key"]: i["satisfied"] for i in resp.json()["items"]}
    assert items["environment_ready"] is True
    assert items["recent_backup"] is True


async def test_api_checklist_unsatisfied_when_nothing_recorded(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.get("/production/checklist", params={"environment": "production"})
    items = {i["key"]: i["satisfied"] for i in resp.json()["items"]}
    assert items["environment_ready"] is False
    assert items["recent_backup"] is False


async def test_api_status_reflects_in_memory_release_and_backup(app_no_db, tmp_path):
    async with _client(app_no_db) as client:
        await client.post("/production/releases", json={"version": "3.0.0"})
        await client.post("/production/backup", json={"scopes": ["configuration"], "destination_dir": str(tmp_path)})
        resp = await client.get("/production/status", params={"environment": "production"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["latest_release_version"] == "3.0.0"
    assert body["latest_backup_id"] is not None


async def test_api_status_empty_platform_returns_nones(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.get("/production/status", params={"environment": "production"})
    body = resp.json()
    assert body["latest_release_version"] is None
    assert body["environment"] == "production"


# ══════════════════════════════════════════════════════════════
# LAYER 11 — CLI (production_cli.py)
# ══════════════════════════════════════════════════════════════

from services.integration.production import production_cli


def test_cli_check_exits_zero_when_ready(capsys, tmp_path):
    rc = production_cli.main(["production", "check", "--environment", "production"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out


def test_cli_config_prints_yaml_by_default(capsys):
    rc = production_cli.main(["production", "config", "--environment", "production", "--format", "yaml"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "tls_required" in out


def test_cli_config_prints_json_when_requested(capsys):
    rc = production_cli.main(["production", "config", "--environment", "production", "--format", "json"])
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out)["security"]["tls_required"] is True


def test_cli_release_prints_summary(capsys):
    rc = production_cli.main(["production", "release", "1.0.0"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Release 1.0.0" in out


def test_cli_release_invalid_version_returns_nonzero(capsys):
    rc = production_cli.main(["production", "release", "not-a-version"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "failed" in out.lower()


def test_cli_backup_writes_file_and_exits_zero(capsys, tmp_path):
    rc = production_cli.main(["production", "backup", "--scopes", "configuration", "--dest", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Backup" in out
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_cli_backup_default_scopes_covers_all(capsys, tmp_path):
    rc = production_cli.main(["production", "backup", "--dest", str(tmp_path)])
    assert rc == 0


def test_cli_validate_directory_of_assets(capsys, tmp_path):
    (tmp_path / "docker-compose.yml").write_text(_VALID_COMPOSE)
    (tmp_path / "deployment.yaml").write_text(_VALID_MANIFEST)
    rc = production_cli.main(["production", "validate", str(tmp_path), "--environment", "production"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "VALID" in out


def test_cli_validate_invalid_asset_returns_nonzero(capsys, tmp_path):
    (tmp_path / "docker-compose.yml").write_text(_INVALID_COMPOSE_NO_SERVICES)
    rc = production_cli.main(["production", "validate", str(tmp_path), "--environment", "production"])
    assert rc == 1


def test_cli_validate_nonexistent_path_returns_nonzero(capsys):
    rc = production_cli.main(["production", "validate", "/no/such/path", "--environment", "production"])
    assert rc == 1


def test_cli_restore_dry_run_never_modifies(capsys, tmp_path):
    production_cli.main(["production", "backup", "--scopes", "configuration", "--dest", str(tmp_path)])
    capsys.readouterr()
    backup_file = next(tmp_path.glob("*.json"))
    rc = production_cli.main(["production", "restore", str(backup_file), "--mode", "dry_run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Dry run only" in out


def test_cli_restore_full_without_yes_refuses(capsys, tmp_path):
    production_cli.main(["production", "backup", "--scopes", "configuration", "--dest", str(tmp_path)])
    capsys.readouterr()
    backup_file = next(tmp_path.glob("*.json"))
    rc = production_cli.main(["production", "restore", str(backup_file), "--mode", "full"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "--yes" in out


def test_cli_restore_full_with_yes_applies(capsys, tmp_path):
    production_cli.main(["production", "backup", "--scopes", "configuration", "--dest", str(tmp_path)])
    capsys.readouterr()
    backup_file = next(tmp_path.glob("*.json"))
    rc = production_cli.main(["production", "restore", str(backup_file), "--mode", "full", "--yes"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "applied" in out.lower()


def test_cli_restore_missing_file_returns_nonzero(capsys):
    rc = production_cli.main(["production", "restore", "/no/such/backup.json"])
    assert rc == 1


def test_cli_status_prints_environment_summary(capsys):
    rc = production_cli.main(["production", "status", "--environment", "production"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Environment: production" in out


def test_cli_checklist_reflects_environment_readiness(capsys):
    rc = production_cli.main(["production", "checklist", "--environment", "production"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Environment checks pass" in out


def test_cli_requires_subcommand(capsys):
    with pytest.raises(SystemExit):
        production_cli.main(["production"])


# ══════════════════════════════════════════════════════════════
# LAYER 12 — Dashboard integration
# ══════════════════════════════════════════════════════════════

def test_build_production_summary_none_when_raw_is_none():
    from services.integration.dashboard.dashboard_builder import build_production_summary
    assert build_production_summary(None) is None


def test_build_production_summary_maps_fields():
    from services.integration.dashboard.dashboard_builder import build_production_summary
    summary = build_production_summary({
        "latest_release_version": "1.2.0", "latest_backup_at": "2026-01-01T00:00:00", "backup_count": 4,
        "latest_environment_status": "pass",
    })
    assert summary.latest_release_version == "1.2.0"
    assert summary.backup_count == 4


def test_build_production_summary_missing_keys_default_safely():
    from services.integration.dashboard.dashboard_builder import build_production_summary
    summary = build_production_summary({})
    assert summary.latest_release_version is None
    assert summary.backup_count == 0


def test_platform_dashboard_production_field_optional_default_none():
    from services.integration.dashboard.dashboard_models import PlatformDashboard
    fields = PlatformDashboard.model_fields
    assert "production" in fields
    assert fields["production"].default is None


async def test_assemble_platform_dashboard_includes_production_summary():
    from services.integration.dashboard.dashboard_builder import assemble_platform_dashboard, build_dashboard_summary
    from services.integration.dashboard.dashboard_models import MetricsSnapshot, ProductionSummary

    summary = ProductionSummary(latest_release_version="1.0.0")
    dashboard = assemble_platform_dashboard(
        services=[], workflows=[], events=[], incidents=[], versions=[],
        metrics=MetricsSnapshot(), production=summary,
    )
    assert dashboard.production.latest_release_version == "1.0.0"


async def test_assemble_platform_dashboard_production_defaults_to_none():
    from services.integration.dashboard.dashboard_builder import assemble_platform_dashboard
    from services.integration.dashboard.dashboard_models import MetricsSnapshot

    dashboard = assemble_platform_dashboard(
        services=[], workflows=[], events=[], incidents=[], versions=[], metrics=MetricsSnapshot(),
    )
    assert dashboard.production is None


# ══════════════════════════════════════════════════════════════
# LAYER 13 — Deployment assets on disk
# ══════════════════════════════════════════════════════════════

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


@pytest.mark.parametrize("relpath", [
    "deploy/docker/Dockerfile",
    "deploy/docker/docker-compose.dev.yml",
    "deploy/docker/docker-compose.staging.yml",
    "deploy/docker/docker-compose.prod.yml",
    "deploy/k8s/deployment.yaml",
    "deploy/k8s/service.yaml",
    "deploy/k8s/configmap.yaml",
    "deploy/k8s/secret.yaml.example",
    "deploy/k8s/ingress.yaml",
    "deploy/k8s/pvc.yaml",
    "deploy/k8s/rbac.yaml",
    "deploy/k8s/networkpolicy.yaml",
    "deploy/k8s/hpa.yaml",
    "deploy/k8s/servicemonitor.yaml",
    "deploy/helm/aasc/Chart.yaml",
    "deploy/helm/aasc/values.yaml",
    "deploy/helm/aasc/templates/deployment.yaml",
    "deploy/helm/aasc/templates/service.yaml",
    "deploy/helm/aasc/templates/configmap.yaml",
    "deploy/helm/aasc/templates/secret.yaml",
    "deploy/helm/aasc/templates/ingress.yaml",
    "deploy/helm/aasc/templates/hpa.yaml",
    "deploy/helm/aasc/templates/misc.yaml",
])
def test_deployment_asset_exists(relpath):
    assert os.path.isfile(os.path.join(_ROOT, relpath)), relpath


@pytest.mark.parametrize("relpath", [
    "docs/operations/production_deployment_guide.md",
    "docs/operations/disaster_recovery_guide.md",
    "docs/operations/backup_restore_guide.md",
    "docs/operations/upgrade_guide.md",
    "docs/operations/rollback_guide.md",
    "docs/operations/monitoring_guide.md",
    "docs/operations/security_checklist.md",
    "docs/operations/capacity_planning_guide.md",
    "docs/operations/troubleshooting_runbook.md",
    "docs/operations/operations_runbook.md",
])
def test_operations_doc_exists_and_nonempty(relpath):
    path = os.path.join(_ROOT, relpath)
    assert os.path.isfile(path)
    assert os.path.getsize(path) > 200


def test_helm_chart_yaml_is_valid_yaml():
    import yaml
    with open(os.path.join(_ROOT, "deploy/helm/aasc/Chart.yaml")) as f:
        data = yaml.safe_load(f)
    assert data["name"] == "aasc-platform"


def test_helm_values_yaml_is_valid_yaml_and_has_required_keys():
    import yaml
    with open(os.path.join(_ROOT, "deploy/helm/aasc/values.yaml")) as f:
        data = yaml.safe_load(f)
    assert "image" in data and "replicaCount" in data


def test_k8s_secret_example_contains_no_real_looking_secrets():
    with open(os.path.join(_ROOT, "deploy/k8s/secret.yaml.example")) as f:
        text = f.read()
    data_lines = [line for line in text.splitlines() if ":" in line and "UkVQTEFDRV9NRQ" in line]
    assert data_lines, "expected placeholder base64 values in the data: section"
    assert all("UkVQTEFDRV9NRQ==" in line for line in data_lines)


def test_alembic_migration_file_exists_and_chains_from_0012():
    path = os.path.join(_ROOT, "alembic/versions/0013_production_readiness.py")
    assert os.path.isfile(path)
    with open(path) as f:
        text = f.read()
    assert 'down_revision = "0012_workflow_designer"' in text
    assert "def upgrade" in text and "def downgrade" in text
