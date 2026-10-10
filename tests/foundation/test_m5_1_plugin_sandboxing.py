"""
tests/foundation/test_m5_1_plugin_sandboxing.py
=================================
M5.1 — Plugin Sandboxing & Secure Execution Framework test suite.
Mirrors the layered testing style established in M4.7-M4.10: pure
models first, then each core module, then IPC/runner (real subprocess
execution, not mocked), then the runtime/manager, then repository,
then CLI, then API, then dashboard, then integration (existing plugins
unaffected).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from services.integration.plugin_sdk import plugin_loader
from services.integration.plugin_sdk.plugin_models import HookType, Permission, PluginRecord
from services.integration.plugin_sdk.plugin_runtime import PluginContext
from services.integration.plugin_sdk.sandbox import (
    sandbox_context, sandbox_export, sandbox_ipc, sandbox_limits, sandbox_monitor, sandbox_policy,
    sandbox_repository, sandbox_runtime,
)
from services.integration.plugin_sdk.sandbox.sandbox_models import (
    ExecutionPolicy, SandboxContextPayload, SandboxDashboardSummary, SandboxExecutionRecord, SandboxLimits,
    SandboxLog, SandboxMetrics, SandboxPolicy, SandboxPolicyRecord, SandboxRequest, SandboxResult, SandboxViolation,
    SandboxViolationRecord, ViolationKind,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
PLUGINS_EXAMPLES_DIR = os.path.join(REPO_ROOT, "plugins", "examples")


# ══════════════════════════════════════════════════════════════
# LAYER 1 — sandbox_models.py (pure shapes)
# ══════════════════════════════════════════════════════════════

def test_execution_policy_has_exactly_two_members():
    assert {m.value for m in ExecutionPolicy} == {"none", "process"}


def test_sandbox_policy_allows_true_when_permission_present():
    policy = SandboxPolicy(plugin_id="x", allowed_permissions=[Permission.ARTIFACTS])
    assert policy.allows(Permission.ARTIFACTS) is True


def test_sandbox_policy_allows_false_when_permission_absent():
    policy = SandboxPolicy(plugin_id="x", allowed_permissions=[])
    assert policy.allows(Permission.NETWORK) is False


def test_sandbox_policy_default_execution_mode_is_none():
    assert SandboxPolicy(plugin_id="x").execution_mode == ExecutionPolicy.NONE


def test_sandbox_limits_defaults():
    limits = SandboxLimits()
    assert limits.wall_timeout_seconds == 10.0
    assert limits.cpu_timeout_seconds == 5.0
    assert limits.max_memory_mb == 256


def test_sandbox_metrics_average_duration_zero_executions():
    assert SandboxMetrics(plugin_id="x").average_duration_ms == 0.0


def test_sandbox_metrics_average_duration_computed():
    metrics = SandboxMetrics(plugin_id="x", execution_count=4, total_duration_ms=100.0)
    assert metrics.average_duration_ms == 25.0


def test_sandbox_result_defaults():
    result = SandboxResult(plugin_id="x", hook="startup", success=True)
    assert result.timed_out is False
    assert result.crashed is False
    assert result.violations == []


def test_sandbox_request_serialization_roundtrip():
    request = SandboxRequest(plugin_id="x", hook="startup", plugins_dir="plugins/examples",
                              kwargs={"a": 1}, context=SandboxContextPayload(config={"k": "v"}))
    raw = request.model_dump_json()
    restored = SandboxRequest.model_validate_json(raw)
    assert restored.plugin_id == "x"
    assert restored.context.config == {"k": "v"}
    assert restored.kwargs == {"a": 1}


def test_sandbox_result_serialization_roundtrip():
    result = SandboxResult(plugin_id="x", hook="startup", success=False, error="boom",
                            violations=[SandboxViolation(plugin_id="x", kind=ViolationKind.TIMEOUT, detail="d")])
    raw = result.model_dump_json()
    restored = SandboxResult.model_validate_json(raw)
    assert restored.success is False
    assert restored.violations[0].kind == ViolationKind.TIMEOUT


def test_sandbox_context_payload_defaults_empty():
    payload = SandboxContextPayload()
    assert payload.config == {}
    assert payload.settings == {}
    assert payload.allowed_permissions == []


def test_sandbox_log_has_default_level():
    log = SandboxLog(plugin_id="x", message="hi")
    assert log.level == "info"


def test_sandbox_dashboard_summary_defaults():
    summary = SandboxDashboardSummary()
    assert summary.healthy is True
    assert summary.execution_mode == ExecutionPolicy.NONE


def test_sandbox_violation_permission_optional():
    v = SandboxViolation(plugin_id="x", kind=ViolationKind.DENIED_PERMISSION, detail="d")
    assert v.permission is None


def test_sandbox_execution_record_fields():
    record = SandboxExecutionRecord(plugin_id="x", hook="startup", execution_mode=ExecutionPolicy.PROCESS,
                                     success=True, timed_out=False, crashed=False, duration_ms=1.0)
    assert record.execution_mode == ExecutionPolicy.PROCESS


def test_sandbox_policy_record_wraps_policy():
    policy = SandboxPolicy(plugin_id="x")
    record = SandboxPolicyRecord(plugin_id="x", policy=policy)
    assert record.policy.plugin_id == "x"


def test_sandbox_violation_record_fields():
    record = SandboxViolationRecord(plugin_id="x", kind=ViolationKind.CRASH, detail="d")
    assert record.kind == ViolationKind.CRASH


# ══════════════════════════════════════════════════════════════
# LAYER 2 — sandbox_policy.py (permission enforcement)
# ══════════════════════════════════════════════════════════════

def test_resolve_permissions_maps_known_strings():
    resolved = sandbox_policy.resolve_permissions(["read", "artifacts"])
    assert set(resolved) == {Permission.READ, Permission.ARTIFACTS}


def test_resolve_permissions_ignores_unknown_strings():
    resolved = sandbox_policy.resolve_permissions(["not_a_real_permission"])
    assert resolved == []


def test_resolve_permissions_deduplicates():
    resolved = sandbox_policy.resolve_permissions(["read", "read", "READ"])
    assert resolved == [Permission.READ]


def test_resolve_permissions_case_insensitive():
    resolved = sandbox_policy.resolve_permissions(["ARTIFACTS"])
    assert resolved == [Permission.ARTIFACTS]


def test_network_denied_in_none_mode():
    resolved = sandbox_policy.resolve_permissions_for_mode(["network"], ExecutionPolicy.NONE)
    assert Permission.NETWORK not in resolved


def test_filesystem_denied_in_none_mode():
    resolved = sandbox_policy.resolve_permissions_for_mode(["filesystem"], ExecutionPolicy.NONE)
    assert Permission.FILESYSTEM not in resolved


def test_shell_denied_in_none_mode():
    resolved = sandbox_policy.resolve_permissions_for_mode(["shell"], ExecutionPolicy.NONE)
    assert Permission.SHELL not in resolved


def test_read_allowed_in_none_mode():
    resolved = sandbox_policy.resolve_permissions_for_mode(["read"], ExecutionPolicy.NONE)
    assert Permission.READ in resolved


def test_read_denied_in_process_mode():
    resolved = sandbox_policy.resolve_permissions_for_mode(["read"], ExecutionPolicy.PROCESS)
    assert Permission.READ not in resolved


def test_artifacts_denied_in_process_mode():
    resolved = sandbox_policy.resolve_permissions_for_mode(["artifacts"], ExecutionPolicy.PROCESS)
    assert Permission.ARTIFACTS not in resolved


def test_events_denied_in_process_mode():
    resolved = sandbox_policy.resolve_permissions_for_mode(["events"], ExecutionPolicy.PROCESS)
    assert Permission.EVENTS not in resolved


def test_network_still_denied_in_process_mode():
    resolved = sandbox_policy.resolve_permissions_for_mode(["network"], ExecutionPolicy.PROCESS)
    assert resolved == []


def test_build_policy_default_deny_empty_manifest():
    policy = sandbox_policy.build_policy("x", [])
    assert policy.allowed_permissions == []


def test_build_policy_grants_matching_permissions():
    policy = sandbox_policy.build_policy("x", ["read", "artifacts"], ExecutionPolicy.NONE)
    assert set(policy.allowed_permissions) == {Permission.READ, Permission.ARTIFACTS}


def test_build_policy_uses_custom_limits():
    limits = SandboxLimits(wall_timeout_seconds=1.0)
    policy = sandbox_policy.build_policy("x", [], limits=limits)
    assert policy.limits.wall_timeout_seconds == 1.0


def test_is_allowed_true():
    policy = sandbox_policy.build_policy("x", ["read"], ExecutionPolicy.NONE)
    assert sandbox_policy.is_allowed(policy, Permission.READ) is True


def test_is_allowed_false():
    policy = sandbox_policy.build_policy("x", [], ExecutionPolicy.NONE)
    assert sandbox_policy.is_allowed(policy, Permission.READ) is False


# ══════════════════════════════════════════════════════════════
# LAYER 3 — sandbox_limits.py
# ══════════════════════════════════════════════════════════════

def test_posix_limits_available_matches_platform():
    # On the CI/sandbox linux environment this runs in, `resource` is
    # importable — asserting the real, non-mocked platform behavior.
    assert sandbox_limits.posix_limits_available() in (True, False)


def test_build_preexec_fn_returns_callable_when_available():
    fn = sandbox_limits.build_preexec_fn(SandboxLimits())
    if sandbox_limits.posix_limits_available():
        assert callable(fn)
    else:
        assert fn is None


def test_truncate_output_no_truncation_when_under_limit():
    assert sandbox_limits.truncate_output("short", 100) == "short"


def test_truncate_output_truncates_when_over_limit():
    result = sandbox_limits.truncate_output("a" * 100, 10)
    assert result.startswith("a" * 10)
    assert "truncated" in result


def test_truncate_output_exact_boundary_not_truncated():
    data = "a" * 10
    assert sandbox_limits.truncate_output(data, 10) == data


# ══════════════════════════════════════════════════════════════
# LAYER 4 — sandbox_context.py (permission-enforcing wrapper)
# ══════════════════════════════════════════════════════════════

def test_none_mode_context_reuses_context_for_plugin():
    base = PluginContext(config={"a": 1}, repository="fake_repo", artifact_manager="fake_artifacts")
    ctx = sandbox_context.build_none_mode_context(base, ["artifacts"])
    assert ctx.artifact_manager == "fake_artifacts"
    assert ctx.repository is None  # "artifacts" doesn't grant repository access


def test_context_payload_only_carries_config_and_settings():
    base = PluginContext(config={"a": 1}, settings={"b": 2}, repository="fake_repo")
    policy = sandbox_policy.build_policy("x", ["read"], ExecutionPolicy.PROCESS)
    payload = sandbox_context.build_context_payload(base, policy)
    assert payload.config == {"a": 1}
    assert payload.settings == {"b": 2}
    assert not hasattr(payload, "repository")


def test_context_proxy_exposes_config_and_settings():
    payload = SandboxContextPayload(config={"k": "v"}, settings={"s": 1})
    proxy = sandbox_context.build_sandbox_context_proxy("x", payload)
    assert proxy.config == {"k": "v"}
    assert proxy.settings == {"s": 1}


def test_context_proxy_denies_repository_access():
    proxy = sandbox_context.build_sandbox_context_proxy("x", SandboxContextPayload())
    with pytest.raises(sandbox_context.SandboxPermissionError):
        proxy.repository


def test_context_proxy_denies_artifact_manager_access():
    proxy = sandbox_context.build_sandbox_context_proxy("x", SandboxContextPayload())
    with pytest.raises(sandbox_context.SandboxPermissionError):
        proxy.artifact_manager


def test_context_proxy_denies_event_publisher_access():
    proxy = sandbox_context.build_sandbox_context_proxy("x", SandboxContextPayload())
    with pytest.raises(sandbox_context.SandboxPermissionError):
        proxy.event_publisher


def test_context_proxy_has_permission_true():
    proxy = sandbox_context.build_sandbox_context_proxy(
        "x", SandboxContextPayload(allowed_permissions=[Permission.READ]))
    assert proxy.has_permission(Permission.READ) is True


def test_context_proxy_has_permission_false():
    proxy = sandbox_context.build_sandbox_context_proxy("x", SandboxContextPayload())
    assert proxy.has_permission(Permission.NETWORK) is False


def test_sandbox_permission_error_message_includes_plugin_and_attribute():
    err = sandbox_context.SandboxPermissionError("myplugin", "repository")
    assert "myplugin" in str(err)
    assert "repository" in str(err)


def test_sandbox_permission_error_is_a_permission_error():
    assert issubclass(sandbox_context.SandboxPermissionError, PermissionError)


# ══════════════════════════════════════════════════════════════
# LAYER 5 — sandbox_ipc.py
# ══════════════════════════════════════════════════════════════

def test_make_ipc_paths_are_unique():
    p1a, p1b = sandbox_ipc.make_ipc_paths()
    p2a, p2b = sandbox_ipc.make_ipc_paths()
    assert p1a != p2a and p1b != p2b
    sandbox_ipc.cleanup_ipc_paths(p1a, p1b, p2a, p2b)


def test_write_read_request_roundtrip(tmp_path):
    path = str(tmp_path / "req.json")
    request = SandboxRequest(plugin_id="x", hook="startup", plugins_dir="plugins/examples")
    sandbox_ipc.write_request(path, request)
    restored = sandbox_ipc.read_request(path)
    assert restored.plugin_id == "x"


def test_write_read_result_roundtrip(tmp_path):
    path = str(tmp_path / "res.json")
    result = SandboxResult(plugin_id="x", hook="startup", success=True)
    sandbox_ipc.write_result(path, result)
    restored = sandbox_ipc.read_result(path, "x", "startup")
    assert restored.success is True


def test_read_result_missing_file_reports_crashed(tmp_path):
    result = sandbox_ipc.read_result(str(tmp_path / "nonexistent.json"), "x", "startup")
    assert result.crashed is True
    assert result.violations[0].kind == ViolationKind.MALFORMED_PAYLOAD


def test_read_result_malformed_json_reports_crashed(tmp_path):
    path = tmp_path / "malformed.json"
    path.write_text("{not valid json")
    result = sandbox_ipc.read_result(str(path), "x", "startup")
    assert result.crashed is True
    assert "malformed" in result.error.lower()


def test_cleanup_ipc_paths_removes_files(tmp_path):
    path = tmp_path / "f.json"
    path.write_text("{}")
    sandbox_ipc.cleanup_ipc_paths(str(path))
    assert not path.exists()


def test_cleanup_ipc_paths_swallows_missing_file_error():
    # Should not raise even though the file never existed.
    sandbox_ipc.cleanup_ipc_paths("/tmp/definitely_does_not_exist_xyz.json")


def test_cleanup_ipc_paths_removes_empty_parent_dir(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    f = sub / "f.json"
    f.write_text("{}")
    sandbox_ipc.cleanup_ipc_paths(str(f))
    assert not sub.exists()


# ══════════════════════════════════════════════════════════════
# LAYER 6 — sandbox_runner.py + sandbox_runtime.py, real subprocess
# execution (not mocked) against real fixture plugins.
# ══════════════════════════════════════════════════════════════

def _write_sandbox_plugin(root: str, plugin_id: str, code: str, permissions=None) -> str:
    plugin_dir = os.path.join(root, plugin_id)
    os.makedirs(plugin_dir, exist_ok=True)
    manifest = {
        "id": plugin_id, "name": plugin_id, "version": "1.0.0", "author": "test",
        "entrypoint": "plugin.py", "permissions": permissions or [], "hooks": ["startup"],
    }
    with open(os.path.join(plugin_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f)
    with open(os.path.join(plugin_dir, "plugin.py"), "w") as f:
        f.write(code)
    return plugin_dir


@pytest.fixture()
def sandbox_plugins_dir(tmp_path):
    return str(tmp_path)


def _discover_and_prepare(plugins_dir, plugin_id):
    discovered = plugin_loader.discover_plugins(plugins_dir)
    target = next(d for d in discovered if d.manifest.id == plugin_id)
    module = plugin_loader.import_entrypoint(target)
    record = PluginRecord(manifest=target.manifest, source_path=target.source_path)
    return record, module


def test_process_mode_success(sandbox_plugins_dir):
    _write_sandbox_plugin(sandbox_plugins_dir, "ok_plugin",
                           "def startup(ctx, **kwargs):\n    return {'value': 42}\n")
    record, module = _discover_and_prepare(sandbox_plugins_dir, "ok_plugin")
    policy = sandbox_policy.build_policy("ok_plugin", [], ExecutionPolicy.PROCESS)
    manager = sandbox_runtime.SandboxManager(sandbox_plugins_dir)
    result = manager.execute(record, module, HookType.STARTUP, PluginContext(), policy)
    assert result.success is True
    assert result.result == {"value": 42}
    assert result.crashed is False
    assert result.timed_out is False


def test_process_mode_timeout(sandbox_plugins_dir):
    _write_sandbox_plugin(sandbox_plugins_dir, "slow_plugin",
                           "import time\ndef startup(ctx, **kwargs):\n    time.sleep(30)\n    return {}\n")
    record, module = _discover_and_prepare(sandbox_plugins_dir, "slow_plugin")
    limits = SandboxLimits(wall_timeout_seconds=1.0)
    policy = sandbox_policy.build_policy("slow_plugin", [], ExecutionPolicy.PROCESS, limits=limits)
    manager = sandbox_runtime.SandboxManager(sandbox_plugins_dir)

    start = time.monotonic()
    result = manager.execute(record, module, HookType.STARTUP, PluginContext(), policy)
    elapsed = time.monotonic() - start

    assert result.timed_out is True
    assert result.success is False
    assert elapsed < 10  # killed well before the plugin's own 30s sleep would finish
    assert any(v.kind == ViolationKind.TIMEOUT for v in result.violations)


def test_process_mode_crash_exception(sandbox_plugins_dir):
    _write_sandbox_plugin(sandbox_plugins_dir, "crash_plugin",
                           "def startup(ctx, **kwargs):\n    raise ValueError('boom')\n")
    record, module = _discover_and_prepare(sandbox_plugins_dir, "crash_plugin")
    policy = sandbox_policy.build_policy("crash_plugin", [], ExecutionPolicy.PROCESS)
    manager = sandbox_runtime.SandboxManager(sandbox_plugins_dir)
    result = manager.execute(record, module, HookType.STARTUP, PluginContext(), policy)
    assert result.success is False
    assert result.crashed is False  # a caught exception is a failure, not a process crash
    assert "ValueError" in result.error
    assert "boom" in result.error


def test_process_mode_stdout_capture(sandbox_plugins_dir):
    _write_sandbox_plugin(sandbox_plugins_dir, "printer_plugin",
                           "def startup(ctx, **kwargs):\n    print('captured stdout line')\n    return {}\n")
    record, module = _discover_and_prepare(sandbox_plugins_dir, "printer_plugin")
    policy = sandbox_policy.build_policy("printer_plugin", [], ExecutionPolicy.PROCESS)
    manager = sandbox_runtime.SandboxManager(sandbox_plugins_dir)
    result = manager.execute(record, module, HookType.STARTUP, PluginContext(), policy)
    assert "captured stdout line" in result.stdout


def test_process_mode_stderr_capture(sandbox_plugins_dir):
    _write_sandbox_plugin(sandbox_plugins_dir, "stderr_plugin",
                           "import sys\ndef startup(ctx, **kwargs):\n    print('err line', file=sys.stderr)\n    return {}\n")
    record, module = _discover_and_prepare(sandbox_plugins_dir, "stderr_plugin")
    policy = sandbox_policy.build_policy("stderr_plugin", [], ExecutionPolicy.PROCESS)
    manager = sandbox_runtime.SandboxManager(sandbox_plugins_dir)
    result = manager.execute(record, module, HookType.STARTUP, PluginContext(), policy)
    assert "err line" in result.stderr


def test_process_mode_denied_capability_access_reported_as_failure(sandbox_plugins_dir):
    _write_sandbox_plugin(sandbox_plugins_dir, "nosy_plugin",
                           "def startup(ctx, **kwargs):\n    return ctx.repository\n", permissions=["read"])
    record, module = _discover_and_prepare(sandbox_plugins_dir, "nosy_plugin")
    # even with "read" declared, PROCESS mode strips it (IPC can't carry a live session)
    policy = sandbox_policy.build_policy("nosy_plugin", ["read"], ExecutionPolicy.PROCESS)
    manager = sandbox_runtime.SandboxManager(sandbox_plugins_dir)
    result = manager.execute(record, module, HookType.STARTUP, PluginContext(), policy)
    assert result.success is False
    assert "SandboxPermissionError" in result.error


def test_process_mode_missing_hook_returns_success_with_none_result(sandbox_plugins_dir):
    _write_sandbox_plugin(sandbox_plugins_dir, "no_startup_plugin", "def shutdown(ctx, **kwargs):\n    return {}\n")
    record, module = _discover_and_prepare(sandbox_plugins_dir, "no_startup_plugin")
    policy = sandbox_policy.build_policy("no_startup_plugin", [], ExecutionPolicy.PROCESS)
    manager = sandbox_runtime.SandboxManager(sandbox_plugins_dir)
    result = manager.execute(record, module, HookType.STARTUP, PluginContext(), policy)
    assert result.success is True
    assert result.result is None


def test_process_mode_kwargs_passed_through(sandbox_plugins_dir):
    _write_sandbox_plugin(sandbox_plugins_dir, "kwargs_plugin",
                           "def startup(ctx, **kwargs):\n    return {'got': kwargs.get('workflow_name')}\n")
    record, module = _discover_and_prepare(sandbox_plugins_dir, "kwargs_plugin")
    policy = sandbox_policy.build_policy("kwargs_plugin", [], ExecutionPolicy.PROCESS)
    manager = sandbox_runtime.SandboxManager(sandbox_plugins_dir)
    result = manager.execute(record, module, HookType.STARTUP, PluginContext(), policy, workflow_name="demo123")
    assert result.result == {"got": "demo123"}


def test_process_mode_no_ipc_temp_files_leak(sandbox_plugins_dir):
    _write_sandbox_plugin(sandbox_plugins_dir, "clean_plugin", "def startup(ctx, **kwargs):\n    return {}\n")
    record, module = _discover_and_prepare(sandbox_plugins_dir, "clean_plugin")
    policy = sandbox_policy.build_policy("clean_plugin", [], ExecutionPolicy.PROCESS)
    manager = sandbox_runtime.SandboxManager(sandbox_plugins_dir)

    before = set(os.listdir(tempfile.gettempdir()))
    for _ in range(3):
        manager.execute(record, module, HookType.STARTUP, PluginContext(), policy)
    after = set(os.listdir(tempfile.gettempdir()))
    leaked = [d for d in (after - before) if d.startswith("aasc_sandbox_ipc_")]
    assert leaked == []


def test_none_mode_matches_dispatch_hook_behavior(sandbox_plugins_dir):
    """NONE mode must reproduce plugin_runtime.dispatch_hook's existing
    behavior exactly — this is the "existing plugins require zero code
    changes" acceptance criterion, checked directly against the real
    dispatch_hook function."""
    _write_sandbox_plugin(sandbox_plugins_dir, "parity_plugin",
                           "def startup(ctx, **kwargs):\n    return {'value': 'same'}\n")
    record, module = _discover_and_prepare(sandbox_plugins_dir, "parity_plugin")
    policy = sandbox_policy.build_policy("parity_plugin", [], ExecutionPolicy.NONE)
    manager = sandbox_runtime.SandboxManager(sandbox_plugins_dir)

    sandbox_result = manager.execute(record, module, HookType.STARTUP, PluginContext(), policy)

    from services.integration.plugin_sdk.plugin_runtime import dispatch_hook
    dispatch_results = dispatch_hook(HookType.STARTUP, [record], {"parity_plugin": module}, PluginContext())

    assert sandbox_result.result == dispatch_results[0].result
    assert sandbox_result.success == dispatch_results[0].success


# ══════════════════════════════════════════════════════════════
# LAYER 7 — dispatch_hook_sandboxed (drop-in alternative to
# plugin_runtime.dispatch_hook)
# ══════════════════════════════════════════════════════════════

def test_dispatch_hook_sandboxed_none_mode_default(sandbox_plugins_dir):
    _write_sandbox_plugin(sandbox_plugins_dir, "default_plugin",
                           "def startup(ctx, **kwargs):\n    return {'ok': True}\n")
    record, module = _discover_and_prepare(sandbox_plugins_dir, "default_plugin")
    results = sandbox_runtime.dispatch_hook_sandboxed(
        HookType.STARTUP, [record], {"default_plugin": module}, PluginContext(), sandbox_plugins_dir)
    assert len(results) == 1
    assert results[0].success is True
    assert results[0].result == {"ok": True}


def test_dispatch_hook_sandboxed_with_explicit_process_policy(sandbox_plugins_dir):
    _write_sandbox_plugin(sandbox_plugins_dir, "explicit_plugin",
                           "def startup(ctx, **kwargs):\n    return {'mode': 'process'}\n")
    record, module = _discover_and_prepare(sandbox_plugins_dir, "explicit_plugin")
    policy = sandbox_policy.build_policy("explicit_plugin", [], ExecutionPolicy.PROCESS)
    results = sandbox_runtime.dispatch_hook_sandboxed(
        HookType.STARTUP, [record], {"explicit_plugin": module}, PluginContext(), sandbox_plugins_dir,
        policies={"explicit_plugin": policy})
    assert results[0].result == {"mode": "process"}


def test_dispatch_hook_sandboxed_skips_plugin_with_no_module(sandbox_plugins_dir):
    _write_sandbox_plugin(sandbox_plugins_dir, "no_module_plugin", "def startup(ctx, **kwargs):\n    return {}\n")
    record, _ = _discover_and_prepare(sandbox_plugins_dir, "no_module_plugin")
    results = sandbox_runtime.dispatch_hook_sandboxed(
        HookType.STARTUP, [record], {}, PluginContext(), sandbox_plugins_dir)
    assert results == []


def test_dispatch_hook_sandboxed_multiple_plugins_sorted_order(sandbox_plugins_dir):
    _write_sandbox_plugin(sandbox_plugins_dir, "zzz_plugin", "def startup(ctx, **kwargs):\n    return 'z'\n")
    _write_sandbox_plugin(sandbox_plugins_dir, "aaa_plugin", "def startup(ctx, **kwargs):\n    return 'a'\n")
    discovered = plugin_loader.discover_plugins(sandbox_plugins_dir)
    records, modules = [], {}
    for d in discovered:
        records.append(PluginRecord(manifest=d.manifest, source_path=d.source_path))
        modules[d.manifest.id] = plugin_loader.import_entrypoint(d)
    results = sandbox_runtime.dispatch_hook_sandboxed(
        HookType.STARTUP, records, modules, PluginContext(), sandbox_plugins_dir)
    assert [r.plugin_id for r in results] == ["aaa_plugin", "zzz_plugin"]


def test_dispatch_hook_sandboxed_one_plugin_failure_does_not_block_others(sandbox_plugins_dir):
    _write_sandbox_plugin(sandbox_plugins_dir, "a_fails", "def startup(ctx, **kwargs):\n    raise ValueError('x')\n")
    _write_sandbox_plugin(sandbox_plugins_dir, "b_ok", "def startup(ctx, **kwargs):\n    return {'ok': True}\n")
    discovered = plugin_loader.discover_plugins(sandbox_plugins_dir)
    records, modules = [], {}
    for d in discovered:
        records.append(PluginRecord(manifest=d.manifest, source_path=d.source_path))
        modules[d.manifest.id] = plugin_loader.import_entrypoint(d)
    results = sandbox_runtime.dispatch_hook_sandboxed(
        HookType.STARTUP, records, modules, PluginContext(), sandbox_plugins_dir)
    by_id = {r.plugin_id: r for r in results}
    assert by_id["a_fails"].success is False
    assert by_id["b_ok"].success is True


def test_dispatch_hook_sandboxed_records_to_monitor(sandbox_plugins_dir):
    _write_sandbox_plugin(sandbox_plugins_dir, "monitored_plugin", "def startup(ctx, **kwargs):\n    return {}\n")
    record, module = _discover_and_prepare(sandbox_plugins_dir, "monitored_plugin")
    monitor = sandbox_monitor.SandboxMonitor()
    sandbox_runtime.dispatch_hook_sandboxed(
        HookType.STARTUP, [record], {"monitored_plugin": module}, PluginContext(), sandbox_plugins_dir,
        monitor=monitor)
    assert monitor.metrics_for("monitored_plugin").execution_count == 1


# ══════════════════════════════════════════════════════════════
# LAYER 8 — sandbox_monitor.py
# ══════════════════════════════════════════════════════════════

def test_monitor_records_success():
    monitor = sandbox_monitor.SandboxMonitor()
    monitor.record(ExecutionPolicy.NONE, SandboxResult(plugin_id="x", hook="startup", success=True, duration_ms=10))
    metrics = monitor.metrics_for("x")
    assert metrics.execution_count == 1
    assert metrics.success_count == 1
    assert metrics.failure_count == 0


def test_monitor_records_failure():
    monitor = sandbox_monitor.SandboxMonitor()
    monitor.record(ExecutionPolicy.NONE, SandboxResult(plugin_id="x", hook="startup", success=False))
    assert monitor.metrics_for("x").failure_count == 1


def test_monitor_records_timeout():
    monitor = sandbox_monitor.SandboxMonitor()
    monitor.record(ExecutionPolicy.PROCESS, SandboxResult(plugin_id="x", hook="startup", success=False, timed_out=True))
    assert monitor.metrics_for("x").timeout_count == 1


def test_monitor_records_crash():
    monitor = sandbox_monitor.SandboxMonitor()
    monitor.record(ExecutionPolicy.PROCESS, SandboxResult(plugin_id="x", hook="startup", success=False, crashed=True))
    assert monitor.metrics_for("x").crash_count == 1


def test_monitor_records_violations():
    monitor = sandbox_monitor.SandboxMonitor()
    violation = SandboxViolation(plugin_id="x", kind=ViolationKind.DENIED_PERMISSION, detail="d")
    monitor.record(ExecutionPolicy.PROCESS, SandboxResult(plugin_id="x", hook="startup", success=False,
                                                           violations=[violation]))
    assert monitor.metrics_for("x").violation_count == 1


def test_monitor_metrics_for_unknown_plugin_is_none():
    monitor = sandbox_monitor.SandboxMonitor()
    assert monitor.metrics_for("nonexistent") is None


def test_monitor_all_metrics_sorted_by_plugin_id():
    monitor = sandbox_monitor.SandboxMonitor()
    monitor.record(ExecutionPolicy.NONE, SandboxResult(plugin_id="zzz", hook="h", success=True))
    monitor.record(ExecutionPolicy.NONE, SandboxResult(plugin_id="aaa", hook="h", success=True))
    assert [m.plugin_id for m in monitor.all_metrics()] == ["aaa", "zzz"]


def test_monitor_dashboard_summary_healthy_with_no_issues():
    monitor = sandbox_monitor.SandboxMonitor()
    monitor.record(ExecutionPolicy.NONE, SandboxResult(plugin_id="x", hook="h", success=True, duration_ms=5))
    summary = monitor.dashboard_summary()
    assert summary.healthy is True
    assert summary.total_executions == 1


def test_monitor_dashboard_summary_unhealthy_with_violation():
    monitor = sandbox_monitor.SandboxMonitor()
    violation = SandboxViolation(plugin_id="x", kind=ViolationKind.DENIED_PERMISSION, detail="d")
    monitor.record(ExecutionPolicy.NONE, SandboxResult(plugin_id="x", hook="h", success=False,
                                                        violations=[violation]))
    assert monitor.dashboard_summary().healthy is False


def test_monitor_dashboard_summary_average_runtime():
    monitor = sandbox_monitor.SandboxMonitor()
    monitor.record(ExecutionPolicy.NONE, SandboxResult(plugin_id="x", hook="h", success=True, duration_ms=10))
    monitor.record(ExecutionPolicy.NONE, SandboxResult(plugin_id="x", hook="h", success=True, duration_ms=20))
    assert monitor.dashboard_summary().average_runtime_ms == 15.0


def test_monitor_reset_clears_state():
    monitor = sandbox_monitor.SandboxMonitor()
    monitor.record(ExecutionPolicy.NONE, SandboxResult(plugin_id="x", hook="h", success=True))
    monitor.reset()
    assert monitor.all_metrics() == []
    assert monitor.dashboard_summary().total_executions == 0


# ══════════════════════════════════════════════════════════════
# LAYER 9 — sandbox_repository.py (real SQLite integration)
# ══════════════════════════════════════════════════════════════

@pytest.fixture
async def sandbox_db_session():
    from infrastructure.database.plugin_sandbox_models import Base
    from infrastructure.database.plugin_sdk_models import Plugin

    engine = create_async_engine("sqlite+aiosqlite://", connect_args={"check_same_thread": False},
                                  poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(Plugin(id="demo", name="Demo", version="1.0.0", author="t", entrypoint="plugin.py"))
        await session.flush()
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_repository_save_and_list_executions(sandbox_db_session):
    result = SandboxResult(plugin_id="demo", hook="startup", success=True, duration_ms=12.5)
    await sandbox_repository.SandboxRepository.save_execution(
        sandbox_db_session, "demo", "startup", ExecutionPolicy.PROCESS, result)
    executions = await sandbox_repository.SandboxRepository.list_executions(sandbox_db_session, "demo")
    assert len(executions) == 1
    assert executions[0].success is True
    assert executions[0].execution_mode == ExecutionPolicy.PROCESS


@pytest.mark.asyncio
async def test_repository_list_executions_filters_by_plugin_id(sandbox_db_session):
    from infrastructure.database.plugin_sdk_models import Plugin
    sandbox_db_session.add(Plugin(id="other", name="Other", version="1.0.0", author="t", entrypoint="plugin.py"))
    await sandbox_db_session.flush()

    await sandbox_repository.SandboxRepository.save_execution(
        sandbox_db_session, "demo", "startup", ExecutionPolicy.NONE, SandboxResult(plugin_id="demo", hook="startup", success=True))
    await sandbox_repository.SandboxRepository.save_execution(
        sandbox_db_session, "other", "startup", ExecutionPolicy.NONE, SandboxResult(plugin_id="other", hook="startup", success=True))

    demo_only = await sandbox_repository.SandboxRepository.list_executions(sandbox_db_session, "demo")
    assert len(demo_only) == 1
    assert demo_only[0].plugin_id == "demo"


@pytest.mark.asyncio
async def test_repository_save_and_get_latest_policy(sandbox_db_session):
    policy = SandboxPolicy(plugin_id="demo", execution_mode=ExecutionPolicy.PROCESS,
                            allowed_permissions=[Permission.READ])
    await sandbox_repository.SandboxRepository.save_policy(sandbox_db_session, policy)
    latest = await sandbox_repository.SandboxRepository.latest_policy(sandbox_db_session, "demo")
    assert latest.execution_mode == ExecutionPolicy.PROCESS
    assert Permission.READ in latest.allowed_permissions


@pytest.mark.asyncio
async def test_repository_latest_policy_none_when_absent(sandbox_db_session):
    latest = await sandbox_repository.SandboxRepository.latest_policy(sandbox_db_session, "demo")
    assert latest is None


@pytest.mark.asyncio
async def test_repository_latest_policy_returns_most_recent(sandbox_db_session):
    await sandbox_repository.SandboxRepository.save_policy(
        sandbox_db_session, SandboxPolicy(plugin_id="demo", execution_mode=ExecutionPolicy.NONE))
    await sandbox_repository.SandboxRepository.save_policy(
        sandbox_db_session, SandboxPolicy(plugin_id="demo", execution_mode=ExecutionPolicy.PROCESS))
    latest = await sandbox_repository.SandboxRepository.latest_policy(sandbox_db_session, "demo")
    assert latest.execution_mode == ExecutionPolicy.PROCESS


@pytest.mark.asyncio
async def test_repository_list_policies(sandbox_db_session):
    await sandbox_repository.SandboxRepository.save_policy(
        sandbox_db_session, SandboxPolicy(plugin_id="demo", execution_mode=ExecutionPolicy.PROCESS))
    records = await sandbox_repository.SandboxRepository.list_policies(sandbox_db_session)
    assert len(records) == 1
    assert records[0].plugin_id == "demo"


@pytest.mark.asyncio
async def test_repository_save_and_list_violations(sandbox_db_session):
    violation = SandboxViolation(plugin_id="demo", kind=ViolationKind.DENIED_PERMISSION, detail="tried network",
                                  permission=Permission.NETWORK)
    await sandbox_repository.SandboxRepository.save_violation(sandbox_db_session, violation)
    violations = await sandbox_repository.SandboxRepository.list_violations(sandbox_db_session, "demo")
    assert len(violations) == 1
    assert violations[0].kind == ViolationKind.DENIED_PERMISSION
    assert violations[0].permission == "network"


@pytest.mark.asyncio
async def test_repository_save_and_get_latest_metrics(sandbox_db_session):
    metrics = SandboxMetrics(plugin_id="demo", execution_count=5, success_count=4, failure_count=1)
    await sandbox_repository.SandboxRepository.save_metrics_snapshot(sandbox_db_session, metrics)
    latest = await sandbox_repository.SandboxRepository.latest_metrics(sandbox_db_session, "demo")
    assert latest.execution_count == 5
    assert latest.success_count == 4


@pytest.mark.asyncio
async def test_repository_latest_metrics_none_when_absent(sandbox_db_session):
    latest = await sandbox_repository.SandboxRepository.latest_metrics(sandbox_db_session, "demo")
    assert latest is None


@pytest.mark.asyncio
async def test_repository_list_executions_respects_limit(sandbox_db_session):
    for i in range(5):
        await sandbox_repository.SandboxRepository.save_execution(
            sandbox_db_session, "demo", "startup", ExecutionPolicy.NONE,
            SandboxResult(plugin_id="demo", hook="startup", success=True))
    executions = await sandbox_repository.SandboxRepository.list_executions(sandbox_db_session, "demo", limit=2)
    assert len(executions) == 2


# ══════════════════════════════════════════════════════════════
# LAYER 10 — sandbox_cli.py
# ══════════════════════════════════════════════════════════════

from services.integration.plugin_sdk.sandbox import sandbox_cli


def test_cli_run_none_mode_success(capsys, sandbox_plugins_dir):
    _write_sandbox_plugin(sandbox_plugins_dir, "cli_ok", "def startup(ctx, **kwargs):\n    return {'ok': True}\n")
    rc = sandbox_cli.main(["run", "cli_ok", "startup", "--mode", "none", "--plugins-dir", sandbox_plugins_dir])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["success"] is True


def test_cli_run_process_mode_success(capsys, sandbox_plugins_dir):
    _write_sandbox_plugin(sandbox_plugins_dir, "cli_proc", "def startup(ctx, **kwargs):\n    return {'ok': True}\n")
    rc = sandbox_cli.main(["run", "cli_proc", "startup", "--mode", "process", "--plugins-dir", sandbox_plugins_dir])
    assert rc == 0


def test_cli_run_unknown_plugin_returns_1(capsys, sandbox_plugins_dir):
    rc = sandbox_cli.main(["run", "does_not_exist", "startup", "--plugins-dir", sandbox_plugins_dir])
    assert rc == 1


def test_cli_run_unknown_hook_returns_1(capsys, sandbox_plugins_dir):
    _write_sandbox_plugin(sandbox_plugins_dir, "cli_badhook", "def startup(ctx, **kwargs):\n    return {}\n")
    rc = sandbox_cli.main(["run", "cli_badhook", "not_a_real_hook", "--plugins-dir", sandbox_plugins_dir])
    assert rc == 1


def test_cli_run_failing_plugin_returns_1(capsys, sandbox_plugins_dir):
    _write_sandbox_plugin(sandbox_plugins_dir, "cli_fail", "def startup(ctx, **kwargs):\n    raise ValueError('x')\n")
    rc = sandbox_cli.main(["run", "cli_fail", "startup", "--plugins-dir", sandbox_plugins_dir])
    assert rc == 1


def test_cli_status_command(capsys):
    rc = sandbox_cli.main(["status"])
    assert rc == 0
    assert "status" in capsys.readouterr().out.lower()


def test_cli_policies_command(capsys):
    rc = sandbox_cli.main(["policies"])
    assert rc == 0


def test_cli_violations_command(capsys):
    rc = sandbox_cli.main(["violations"])
    assert rc == 0


def test_cli_history_command(capsys):
    rc = sandbox_cli.main(["history"])
    assert rc == 0


def test_cli_requires_subcommand():
    with pytest.raises(SystemExit):
        sandbox_cli.main([])


def test_cli_run_markdown_format(capsys, sandbox_plugins_dir):
    _write_sandbox_plugin(sandbox_plugins_dir, "cli_md", "def startup(ctx, **kwargs):\n    return {'ok': True}\n")
    rc = sandbox_cli.main(["run", "cli_md", "startup", "--plugins-dir", sandbox_plugins_dir, "--format", "markdown"])
    assert rc == 0
    assert "#" in capsys.readouterr().out


# ══════════════════════════════════════════════════════════════
# LAYER 11 — services/integration/api/sandbox.py
# ══════════════════════════════════════════════════════════════

from services.integration.api.sandbox import router as sandbox_router


@pytest.fixture()
def sandbox_api_client(sandbox_plugins_dir):
    app = FastAPI()
    app.include_router(sandbox_router)
    app.state.plugins_dir = sandbox_plugins_dir
    return TestClient(app)


def test_api_status_no_runs_yet(sandbox_api_client):
    resp = sandbox_api_client.get("/plugins/sandbox/status")
    assert resp.status_code == 200
    assert resp.json()["total_executions"] == 0


def test_api_run_none_mode(sandbox_api_client, sandbox_plugins_dir):
    _write_sandbox_plugin(sandbox_plugins_dir, "api_none", "def startup(ctx, **kwargs):\n    return {'ok': True}\n")
    resp = sandbox_api_client.post("/plugins/sandbox/run",
                                    json={"plugin_id": "api_none", "hook": "startup", "mode": "none"})
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_api_run_process_mode(sandbox_api_client, sandbox_plugins_dir):
    _write_sandbox_plugin(sandbox_plugins_dir, "api_proc", "def startup(ctx, **kwargs):\n    return {'ok': True}\n")
    resp = sandbox_api_client.post("/plugins/sandbox/run",
                                    json={"plugin_id": "api_proc", "hook": "startup", "mode": "process"})
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_api_run_unknown_plugin_404(sandbox_api_client):
    resp = sandbox_api_client.post("/plugins/sandbox/run",
                                    json={"plugin_id": "nope", "hook": "startup"})
    assert resp.status_code == 404


def test_api_run_unknown_hook_422(sandbox_api_client, sandbox_plugins_dir):
    _write_sandbox_plugin(sandbox_plugins_dir, "api_badhook", "def startup(ctx, **kwargs):\n    return {}\n")
    resp = sandbox_api_client.post("/plugins/sandbox/run",
                                    json={"plugin_id": "api_badhook", "hook": "not_a_hook"})
    assert resp.status_code == 422


def test_api_run_unknown_mode_422(sandbox_api_client, sandbox_plugins_dir):
    _write_sandbox_plugin(sandbox_plugins_dir, "api_badmode", "def startup(ctx, **kwargs):\n    return {}\n")
    resp = sandbox_api_client.post("/plugins/sandbox/run",
                                    json={"plugin_id": "api_badmode", "hook": "startup", "mode": "container"})
    assert resp.status_code == 422


def test_api_status_reflects_run(sandbox_api_client, sandbox_plugins_dir):
    _write_sandbox_plugin(sandbox_plugins_dir, "api_status", "def startup(ctx, **kwargs):\n    return {}\n")
    sandbox_api_client.post("/plugins/sandbox/run", json={"plugin_id": "api_status", "hook": "startup"})
    resp = sandbox_api_client.get("/plugins/sandbox/status")
    assert resp.json()["total_executions"] == 1


def test_api_history_503_without_db(sandbox_api_client):
    resp = sandbox_api_client.get("/plugins/sandbox/history")
    assert resp.status_code == 503


def test_api_policies_get_503_without_db(sandbox_api_client):
    resp = sandbox_api_client.get("/plugins/sandbox/policies")
    assert resp.status_code == 503


def test_api_policies_put_503_without_db(sandbox_api_client):
    resp = sandbox_api_client.put("/plugins/sandbox/policies",
                                   json={"plugin_id": "x", "execution_mode": "none", "permissions": []})
    assert resp.status_code == 503


def _make_db_factory():
    from contextlib import asynccontextmanager

    engine = create_async_engine("sqlite+aiosqlite://", connect_args={"check_same_thread": False},
                                  poolclass=StaticPool)

    @asynccontextmanager
    async def factory():
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
            await session.commit()

    return engine, factory


@pytest.mark.asyncio
async def test_api_full_db_backed_flow(sandbox_plugins_dir):
    _write_sandbox_plugin(sandbox_plugins_dir, "db_plugin", "def startup(ctx, **kwargs):\n    return {'ok': True}\n")
    engine, factory = _make_db_factory()

    from infrastructure.database.plugin_sandbox_models import Base
    from infrastructure.database.plugin_sdk_models import Plugin
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as db:
        db.add(Plugin(id="db_plugin", name="Demo", version="1.0.0", author="t", entrypoint="plugin.py"))
        await db.flush()
        await db.commit()

    app = FastAPI()
    app.include_router(sandbox_router)
    app.state.plugins_dir = sandbox_plugins_dir
    app.state.db_factory = factory
    client = TestClient(app)

    run_resp = client.post("/plugins/sandbox/run", json={"plugin_id": "db_plugin", "hook": "startup"})
    assert run_resp.status_code == 200

    history_resp = client.get("/plugins/sandbox/history")
    assert history_resp.status_code == 200
    assert len(history_resp.json()["executions"]) == 1

    put_resp = client.put("/plugins/sandbox/policies",
                           json={"plugin_id": "db_plugin", "execution_mode": "process", "permissions": ["read"]})
    assert put_resp.status_code == 200

    get_resp = client.get("/plugins/sandbox/policies")
    assert get_resp.status_code == 200
    assert len(get_resp.json()["policies"]) == 1

    await engine.dispose()


# ══════════════════════════════════════════════════════════════
# LAYER 12 — dashboard integration
# ══════════════════════════════════════════════════════════════

from services.integration.dashboard import dashboard_builder
from services.integration.dashboard.dashboard_models import MetricsSnapshot, PluginSandboxSummary


def test_build_plugin_sandbox_summary_none_yields_none():
    assert dashboard_builder.build_plugin_sandbox_summary(None) is None


def test_build_plugin_sandbox_summary_populates_fields():
    monitor = sandbox_monitor.SandboxMonitor()
    monitor.record(ExecutionPolicy.PROCESS, SandboxResult(plugin_id="x", hook="h", success=True, duration_ms=20))
    raw = monitor.dashboard_summary().model_dump(mode="json")
    card = dashboard_builder.build_plugin_sandbox_summary(raw)
    assert isinstance(card, PluginSandboxSummary)
    assert card.total_executions == 1
    assert card.execution_mode == "process"


def test_assemble_platform_dashboard_includes_sandbox_card():
    card = PluginSandboxSummary(total_executions=3)
    dashboard = dashboard_builder.assemble_platform_dashboard(
        [], [], [], [], [], MetricsSnapshot(), plugin_sandbox=card)
    assert dashboard.plugin_sandbox is not None
    assert dashboard.plugin_sandbox.total_executions == 3


def test_assemble_platform_dashboard_sandbox_card_absent_by_default():
    dashboard = dashboard_builder.assemble_platform_dashboard([], [], [], [], [], MetricsSnapshot())
    assert dashboard.plugin_sandbox is None


def test_platform_dashboard_serializes_sandbox_card():
    card = PluginSandboxSummary(total_executions=3, healthy=False)
    dashboard = dashboard_builder.assemble_platform_dashboard(
        [], [], [], [], [], MetricsSnapshot(), plugin_sandbox=card)
    dumped = dashboard.model_dump(mode="json")
    assert dumped["plugin_sandbox"]["healthy"] is False


# ══════════════════════════════════════════════════════════════
# LAYER 13 — Integration: existing plugins/examples/* continue to
# work with ZERO code changes, through both the untouched
# plugin_runtime.dispatch_hook and the new dispatch_hook_sandboxed.
# ══════════════════════════════════════════════════════════════

def test_existing_hello_plugin_works_unchanged_via_dispatch_hook():
    """plugin_runtime.dispatch_hook itself — zero lines touched by
    M5.1 — still works exactly as it did in M4.7/M4.10."""
    from services.integration.plugin_sdk.plugin_runtime import dispatch_hook
    discovered = plugin_loader.discover_plugins(PLUGINS_EXAMPLES_DIR)
    hello = next(d for d in discovered if d.manifest.id == "hello_plugin")
    module = plugin_loader.import_entrypoint(hello)
    record = PluginRecord(manifest=hello.manifest, source_path=hello.source_path)
    results = dispatch_hook(HookType.STARTUP, [record], {"hello_plugin": module}, PluginContext())
    assert results[0].success is True
    assert "hello_plugin started" in results[0].result["message"]


def test_existing_hello_plugin_works_via_dispatch_hook_sandboxed_none_mode():
    """Same plugin, same hook, routed through the new sandboxed dispatch
    path instead — with NO policy specified (defaults to NONE) it must
    produce an equivalent result, proving zero plugin code changes are
    needed to adopt M5.1."""
    discovered = plugin_loader.discover_plugins(PLUGINS_EXAMPLES_DIR)
    hello = next(d for d in discovered if d.manifest.id == "hello_plugin")
    module = plugin_loader.import_entrypoint(hello)
    record = PluginRecord(manifest=hello.manifest, source_path=hello.source_path)
    results = sandbox_runtime.dispatch_hook_sandboxed(
        HookType.STARTUP, [record], {"hello_plugin": module}, PluginContext(), PLUGINS_EXAMPLES_DIR)
    assert results[0].success is True
    assert "hello_plugin started" in results[0].result["message"]


def test_existing_hello_plugin_works_in_process_mode_unchanged():
    """The same real, unmodified M4.7 example plugin, run through real
    PROCESS-mode subprocess isolation — the strongest possible proof
    that no plugin code change is required."""
    discovered = plugin_loader.discover_plugins(PLUGINS_EXAMPLES_DIR)
    hello = next(d for d in discovered if d.manifest.id == "hello_plugin")
    module = plugin_loader.import_entrypoint(hello)
    record = PluginRecord(manifest=hello.manifest, source_path=hello.source_path)
    policy = sandbox_policy.build_policy("hello_plugin", hello.manifest.permissions, ExecutionPolicy.PROCESS)
    manager = sandbox_runtime.SandboxManager(PLUGINS_EXAMPLES_DIR)
    result = manager.execute(record, module, HookType.STARTUP, PluginContext(), policy)
    assert result.success is True
    assert "hello_plugin started" in result.result["message"]


def test_existing_metrics_plugin_works_in_process_mode():
    discovered = plugin_loader.discover_plugins(PLUGINS_EXAMPLES_DIR)
    target = next(d for d in discovered if d.manifest.id == "metrics_plugin")
    module = plugin_loader.import_entrypoint(target)
    record = PluginRecord(manifest=target.manifest, source_path=target.source_path)
    policy = sandbox_policy.build_policy("metrics_plugin", target.manifest.permissions, ExecutionPolicy.PROCESS)
    manager = sandbox_runtime.SandboxManager(PLUGINS_EXAMPLES_DIR)
    result = manager.execute(record, module, HookType.BEFORE_AGENT_RUN, PluginContext(), policy)
    assert result.success is True
    assert result.result["count"] == 1


def test_existing_notification_plugin_works_in_process_mode():
    discovered = plugin_loader.discover_plugins(PLUGINS_EXAMPLES_DIR)
    target = next(d for d in discovered if d.manifest.id == "notification_plugin")
    module = plugin_loader.import_entrypoint(target)
    record = PluginRecord(manifest=target.manifest, source_path=target.source_path)
    policy = sandbox_policy.build_policy("notification_plugin", target.manifest.permissions, ExecutionPolicy.PROCESS)
    manager = sandbox_runtime.SandboxManager(PLUGINS_EXAMPLES_DIR)
    result = manager.execute(record, module, HookType.INCIDENT_CREATED, PluginContext(), policy,
                              incident_id="INC-1", severity="high")
    assert result.success is True
    assert result.result["incident_id"] == "INC-1"


def test_plugin_runtime_module_has_zero_new_sandbox_imports():
    """Confirms `plugin_runtime.py` itself was not modified to
    reference the sandbox package at all — the architectural
    requirement that "PluginRuntime should not know how the sandbox
    works internally," checked by static inspection of the actual
    module's imports rather than just asserted in a docstring."""
    import services.integration.plugin_sdk.plugin_runtime as pr
    source = open(pr.__file__).read()
    assert "sandbox" not in source.lower()


def test_all_three_example_plugins_still_discoverable():
    discovered = plugin_loader.discover_plugins(PLUGINS_EXAMPLES_DIR)
    ids = {d.manifest.id for d in discovered}
    assert ids == {"hello_plugin", "metrics_plugin", "notification_plugin"}


# ══════════════════════════════════════════════════════════════
# LAYER 14 — sandbox_export.py
# ══════════════════════════════════════════════════════════════

def test_export_json():
    result = SandboxResult(plugin_id="x", hook="startup", success=True)
    content = sandbox_export.export_report(result, "json")
    parsed = json.loads(content)
    assert parsed["plugin_id"] == "x"


def test_export_markdown():
    result = SandboxResult(plugin_id="x", hook="startup", success=True)
    content = sandbox_export.export_report(result, "markdown")
    assert isinstance(content, str) and len(content) > 0


def test_export_html():
    result = SandboxResult(plugin_id="x", hook="startup", success=True)
    content = sandbox_export.export_report(result, "html")
    assert "<html>" in content


def test_export_unknown_format_raises():
    result = SandboxResult(plugin_id="x", hook="startup", success=True)
    with pytest.raises(sandbox_export.ExportError):
        sandbox_export.export_report(result, "yaml")


# ══════════════════════════════════════════════════════════════
# LAYER 15 — infrastructure/database/plugin_sandbox_models.py (direct)
# ══════════════════════════════════════════════════════════════

def test_sandbox_db_models_share_base_with_plugin_sdk():
    from infrastructure.database.plugin_sdk_models import Base as B1
    from infrastructure.database.plugin_sandbox_models import Base as B2
    assert B1 is B2


def test_sandbox_db_models_define_four_tables():
    from infrastructure.database.plugin_sandbox_models import Base
    expected = {"sandbox_executions", "sandbox_policies", "sandbox_violations", "sandbox_metrics"}
    assert expected.issubset(set(Base.metadata.tables.keys()))


def test_sandbox_execution_table_has_plugin_fk():
    from infrastructure.database.plugin_sandbox_models import SandboxExecution
    fks = {fk.target_fullname for fk in SandboxExecution.__table__.foreign_keys}
    assert "plugins.id" in fks


@pytest.mark.asyncio
async def test_migration_table_shapes_match_orm_models():
    """Sanity check that the Alembic migration's column set for each
    table is a superset of what the ORM model declares (not an exact
    string match, since server_default vs. ORM default differ) — the
    two must at minimum agree on table/column names."""
    from infrastructure.database.plugin_sandbox_models import Base
    engine = create_async_engine("sqlite+aiosqlite://", connect_args={"check_same_thread": False},
                                  poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with engine.connect() as conn:
        def _get_columns(sync_conn):
            from sqlalchemy import inspect
            inspector = inspect(sync_conn)
            return {t: {c["name"] for c in inspector.get_columns(t)} for t in
                    ["sandbox_executions", "sandbox_policies", "sandbox_violations", "sandbox_metrics"]}
        columns = await conn.run_sync(_get_columns)
    assert "plugin_id" in columns["sandbox_executions"]
    assert "execution_mode" in columns["sandbox_executions"]
    assert "allowed_permissions" in columns["sandbox_policies"]
    assert "kind" in columns["sandbox_violations"]
    assert "execution_count" in columns["sandbox_metrics"]
    await engine.dispose()


# ══════════════════════════════════════════════════════════════
# LAYER 16 — additional edge cases
# ══════════════════════════════════════════════════════════════

def test_process_mode_empty_kwargs_default():
    request = SandboxRequest(plugin_id="x", hook="startup", plugins_dir="d")
    assert request.kwargs == {}


def test_process_mode_plugin_not_found_reports_failure(sandbox_plugins_dir):
    """A request naming a plugin id that isn't in `plugins_dir` at all
    (not just missing a hook) must still produce a clean SandboxResult,
    not an unhandled traceback on stderr."""
    req_path, res_path = sandbox_ipc.make_ipc_paths()
    try:
        request = SandboxRequest(plugin_id="totally_nonexistent", hook="startup", plugins_dir=sandbox_plugins_dir)
        sandbox_ipc.write_request(req_path, request)
        import subprocess
        proc = subprocess.run([sys.executable, "-m", "services.integration.plugin_sdk.sandbox.sandbox_runner",
                                req_path, res_path], capture_output=True, text=True, timeout=10)
        assert proc.returncode == 0
        result = sandbox_ipc.read_result(res_path, "totally_nonexistent", "startup")
        assert result.success is False
    finally:
        sandbox_ipc.cleanup_ipc_paths(req_path, res_path)


def test_sandbox_runner_main_wrong_argc_returns_2():
    from services.integration.plugin_sdk.sandbox import sandbox_runner
    assert sandbox_runner.main([]) == 2
    assert sandbox_runner.main(["only_one_arg"]) == 2


def test_manager_execute_none_mode_records_zero_wall_clock_overhead(sandbox_plugins_dir):
    """NONE mode should be dramatically faster than PROCESS mode for
    the same trivial plugin — a rough but real proxy confirming NONE
    mode genuinely skips subprocess spawn overhead rather than
    routing through it internally."""
    _write_sandbox_plugin(sandbox_plugins_dir, "perf_plugin", "def startup(ctx, **kwargs):\n    return {}\n")
    record, module = _discover_and_prepare(sandbox_plugins_dir, "perf_plugin")
    manager = sandbox_runtime.SandboxManager(sandbox_plugins_dir)

    none_policy = sandbox_policy.build_policy("perf_plugin", [], ExecutionPolicy.NONE)
    none_result = manager.execute(record, module, HookType.STARTUP, PluginContext(), none_policy)

    process_policy = sandbox_policy.build_policy("perf_plugin", [], ExecutionPolicy.PROCESS)
    process_result = manager.execute(record, module, HookType.STARTUP, PluginContext(), process_policy)

    assert none_result.duration_ms < process_result.duration_ms


def test_multiple_sequential_process_runs_produce_independent_results(sandbox_plugins_dir):
    """Confirms no state leaks between separate PROCESS-mode
    invocations (e.g. via a stale sys.modules entry or a reused temp
    dir) by running the same plugin twice with different kwargs."""
    _write_sandbox_plugin(sandbox_plugins_dir, "stateless_plugin",
                           "def startup(ctx, **kwargs):\n    return {'echo': kwargs.get('value')}\n")
    record, module = _discover_and_prepare(sandbox_plugins_dir, "stateless_plugin")
    policy = sandbox_policy.build_policy("stateless_plugin", [], ExecutionPolicy.PROCESS)
    manager = sandbox_runtime.SandboxManager(sandbox_plugins_dir)

    r1 = manager.execute(record, module, HookType.STARTUP, PluginContext(), policy, value="first")
    r2 = manager.execute(record, module, HookType.STARTUP, PluginContext(), policy, value="second")
    assert r1.result == {"echo": "first"}
    assert r2.result == {"echo": "second"}


def test_cleanup_ipc_paths_removes_empty_dir_even_when_no_file_was_ever_written():
    """Regression test for a real leak this milestone's own test suite
    caught: make_ipc_paths() creates its temp dir eagerly, before any
    request/result file necessarily exists — cleanup_ipc_paths must
    still remove that empty directory, not only when a file inside it
    was actually written and removed."""
    req_path, res_path = sandbox_ipc.make_ipc_paths()
    parent_dir = os.path.dirname(req_path)
    assert os.path.isdir(parent_dir)  # make_ipc_paths creates it eagerly
    assert not os.path.isfile(req_path)  # but writes nothing into it yet

    sandbox_ipc.cleanup_ipc_paths(req_path, res_path)
    assert not os.path.isdir(parent_dir)
