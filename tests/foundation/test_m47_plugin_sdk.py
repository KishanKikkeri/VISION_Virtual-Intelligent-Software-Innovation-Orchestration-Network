"""
tests/foundation/test_m47_plugin_sdk.py
=================================
M4.7 Plugin SDK & Extension Framework tests.

Layer 1  — Models:             plugin_models construction/defaults, enum coverage.
Layer 2  — Manifest:           JSON/TOML parsing, required-field/malformed-content errors.
Layer 3  — Validator:          permissions, hooks, version compatibility, duplicate ids,
                                dependency graph (order + missing deps + cycles), validate_all.
Layer 4  — Loader:             discover_plugins (python_package/zip_bundle/editable),
                                import_entrypoint, validate_entrypoint_importable, cleanup_extracted.
Layer 5  — Registry:           install/enable/disable/remove/mark_error, list_* views,
                                capability lookup, has_permission.
Layer 6  — Runtime:            PluginContext, context_for_plugin permission scoping,
                                dispatch_hook determinism + failure isolation, compute_health.
Layer 7  — Repository:         plugin_repository against a real in-memory SQLite async engine.
Layer 8  — Export:             inventory / health / validation, json/markdown/html.
Layer 9  — Dashboard:          fetch_plugin_dashboard_section, build_plugins_summary,
                                PlatformDashboard.plugins wiring.
Layer 10 — API:                every /plugins* route, in-memory + DB-backed.
Layer 11 — CLI:                plugin_cli.main against a synthetic plugins directory.
Layer 12 — Regression/isolation.
"""
from __future__ import annotations

import json
import os
import tempfile
import zipfile
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from services.integration.plugin_sdk import (
    plugin_export, plugin_loader, plugin_manifest, plugin_validator,
)
from services.integration.plugin_sdk.plugin_models import (
    DependencyGraphResult, HookType, Permission, PluginDependency, PluginManifest, PluginRecord, PluginSourceType,
    PluginState, ValidationIssue,
)
from services.integration.plugin_sdk.plugin_registry import PluginAlreadyInstalledError, PluginNotFoundError, PluginRegistry
from services.integration.plugin_sdk.plugin_runtime import PluginContext, compute_health, context_for_plugin, dispatch_hook

# ══════════════════════════════════════════════════════════════
# Fixtures / helpers
# ══════════════════════════════════════════════════════════════

_GOOD_ENTRYPOINT = """
def before_workflow(ctx, **kwargs):
    return {"ok": True, "has_repo": ctx.repository is not None}

def after_workflow(ctx, **kwargs):
    raise RuntimeError("simulated plugin failure")
"""


def _write_manifest(directory: str, manifest_dict: dict, filename: str = "manifest.json") -> None:
    with open(os.path.join(directory, filename), "w", encoding="utf-8") as fh:
        if filename.endswith(".toml"):
            # minimal, flat TOML writer sufficient for this manifest shape
            lines = []
            for key, value in manifest_dict.items():
                if isinstance(value, str):
                    lines.append(f'{key} = "{value}"')
                elif isinstance(value, list) and (not value or isinstance(value[0], str)):
                    quoted = ", ".join(f'"{v}"' for v in value)
                    lines.append(f"{key} = [{quoted}]")
            fh.write("\n".join(lines))
        else:
            json.dump(manifest_dict, fh)


def _manifest_dict(plugin_id: str = "demo-plugin", **overrides) -> dict:
    base = {
        "id": plugin_id, "name": "Demo Plugin", "version": "1.0.0", "author": "Acme",
        "description": "A demo plugin.", "entrypoint": "plugin.py", "api_version": "1.0.0",
        "permissions": ["read", "events"], "dependencies": [], "hooks": ["before_workflow", "after_workflow"],
    }
    base.update(overrides)
    return base


def _write_plugin_package(root: str, plugin_id: str = "demo-plugin", entrypoint_code: str = _GOOD_ENTRYPOINT,
                           **manifest_overrides) -> str:
    plugin_dir = os.path.join(root, plugin_id)
    os.makedirs(plugin_dir, exist_ok=True)
    _write_manifest(plugin_dir, _manifest_dict(plugin_id, **manifest_overrides))
    with open(os.path.join(plugin_dir, "plugin.py"), "w", encoding="utf-8") as fh:
        fh.write(entrypoint_code)
    return plugin_dir


def _write_plugin_zip(root: str, plugin_id: str = "zip-plugin") -> str:
    zip_path = os.path.join(root, f"{plugin_id}.zip")
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(_manifest_dict(plugin_id)))
        zf.writestr("plugin.py", _GOOD_ENTRYPOINT)
    return zip_path


def _f(rule_id: str = "R1", severity: str = "error") -> ValidationIssue:
    return ValidationIssue(rule_id=rule_id, severity=severity, message="m")


def _manifest(plugin_id: str = "p1", dependencies: Optional[List[PluginDependency]] = None,
              permissions: Optional[List[str]] = None, hooks: Optional[List[str]] = None,
              api_version: str = "1.0.0") -> PluginManifest:
    return PluginManifest(
        id=plugin_id, name=plugin_id, version="1.0.0", author="a", entrypoint="plugin.py",
        api_version=api_version, permissions=permissions or [], hooks=hooks or [],
        dependencies=dependencies or [],
    )


# ══════════════════════════════════════════════════════════════
# LAYER 1 — Models
# ══════════════════════════════════════════════════════════════

def test_permission_enum_values():
    assert {p.value for p in Permission} == {"read", "write", "events", "artifacts", "network", "filesystem", "shell"}


def test_hook_type_enum_values():
    assert {h.value for h in HookType} == {
        "before_agent_run", "after_agent_run", "before_workflow", "after_workflow", "artifact_created",
        "deployment_completed", "incident_created", "security_scan_finished", "dashboard_section",
        "startup", "shutdown",
    }


def test_plugin_manifest_defaults():
    m = PluginManifest(id="x", name="X", version="1.0", author="me", entrypoint="plugin.py")
    assert m.permissions == []
    assert m.hooks == []
    assert m.api_version == "1.0.0"


def test_validation_result_errors_and_warnings_properties():
    result = plugin_validator.ValidationResult(plugin_id="p1", issues=[_f(severity="error"), _f(severity="warning")])
    assert len(result.errors) == 1
    assert len(result.warnings) == 1


def test_plugin_manifest_json_roundtrip():
    m = _manifest("roundtrip", dependencies=[PluginDependency(plugin_id="dep", version_constraint=">=1.0")])
    restored = PluginManifest.model_validate(json.loads(json.dumps(m.model_dump(mode="json"))))
    assert restored.dependencies[0].plugin_id == "dep"


# ══════════════════════════════════════════════════════════════
# LAYER 2 — Manifest
# ══════════════════════════════════════════════════════════════

def test_load_manifest_json_valid():
    m = plugin_manifest.load_manifest_json(json.dumps(_manifest_dict()))
    assert m.id == "demo-plugin"
    assert m.permissions == ["read", "events"]


def test_load_manifest_json_malformed_raises():
    with pytest.raises(plugin_manifest.ManifestError):
        plugin_manifest.load_manifest_json("{not json")


def test_load_manifest_json_missing_required_field_raises():
    data = _manifest_dict()
    del data["entrypoint"]
    with pytest.raises(plugin_manifest.ManifestError):
        plugin_manifest.load_manifest_json(json.dumps(data))


def test_load_manifest_json_not_an_object_raises():
    with pytest.raises(plugin_manifest.ManifestError):
        plugin_manifest.load_manifest_json(json.dumps([1, 2, 3]))


def test_load_manifest_toml_valid():
    toml_text = 'id = "toml-plugin"\nname = "Toml"\nversion = "1.0.0"\nauthor = "me"\nentrypoint = "plugin.py"\n'
    m = plugin_manifest.load_manifest_toml(toml_text)
    assert m.id == "toml-plugin"


def test_load_manifest_toml_malformed_raises():
    with pytest.raises(plugin_manifest.ManifestError):
        plugin_manifest.load_manifest_toml("this is not [valid toml")


def test_load_manifest_dispatches_by_extension():
    json_content = json.dumps(_manifest_dict())
    assert plugin_manifest.load_manifest(json_content, "manifest.json").id == "demo-plugin"


def test_load_manifest_unrecognized_extension_raises():
    with pytest.raises(plugin_manifest.ManifestError):
        plugin_manifest.load_manifest("irrelevant", "manifest.yaml")


# ══════════════════════════════════════════════════════════════
# LAYER 3 — Validator
# ══════════════════════════════════════════════════════════════

def test_validate_permissions_flags_unknown():
    m = _manifest(permissions=["read", "bogus"])
    issues = plugin_validator.validate_permissions(m)
    assert len(issues) == 1
    assert issues[0].rule_id == "PLUGIN-UNKNOWN-PERMISSION"


def test_validate_permissions_all_known_passes():
    m = _manifest(permissions=["read", "write", "events"])
    assert plugin_validator.validate_permissions(m) == []


def test_validate_hooks_flags_unknown():
    m = _manifest(hooks=["before_workflow", "not_a_real_hook"])
    issues = plugin_validator.validate_hooks(m)
    assert len(issues) == 1
    assert issues[0].rule_id == "PLUGIN-UNKNOWN-HOOK"


def test_validate_version_compatibility_major_mismatch():
    m = _manifest(api_version="2.0.0")
    issues = plugin_validator.validate_version_compatibility(m, platform_api_version="1.0.0")
    assert len(issues) == 1
    assert issues[0].rule_id == "PLUGIN-API-VERSION-INCOMPATIBLE"


def test_validate_version_compatibility_minor_mismatch_ok():
    m = _manifest(api_version="1.5.0")
    assert plugin_validator.validate_version_compatibility(m, platform_api_version="1.0.0") == []


def test_validate_manifest_aggregates_all_single_plugin_checks():
    m = _manifest(permissions=["bogus"], hooks=["nope"], api_version="9.0.0")
    result = plugin_validator.validate_manifest(m)
    assert not result.valid
    assert len(result.issues) == 3


def test_validate_duplicate_ids_detects_collision():
    manifests = [_manifest("dup"), _manifest("dup"), _manifest("unique")]
    dup_issues = plugin_validator.validate_duplicate_ids(manifests)
    assert "dup" in dup_issues
    assert "unique" not in dup_issues


def test_build_dependency_graph_simple_order():
    a = _manifest("a")
    b = _manifest("b", dependencies=[PluginDependency(plugin_id="a")])
    c = _manifest("c", dependencies=[PluginDependency(plugin_id="b")])
    graph = plugin_validator.build_dependency_graph([c, b, a])
    assert graph.order == ["a", "b", "c"]
    assert graph.cycles == []
    assert graph.missing_dependencies == {}


def test_build_dependency_graph_missing_dependency():
    a = _manifest("a", dependencies=[PluginDependency(plugin_id="ghost")])
    graph = plugin_validator.build_dependency_graph([a])
    assert graph.missing_dependencies == {"a": ["ghost"]}
    assert "a" in graph.order  # a itself has no *present* incoming edges, so it still gets ordered


def test_build_dependency_graph_detects_simple_cycle():
    a = _manifest("a", dependencies=[PluginDependency(plugin_id="b")])
    b = _manifest("b", dependencies=[PluginDependency(plugin_id="a")])
    graph = plugin_validator.build_dependency_graph([a, b])
    assert graph.cycles
    assert set(graph.cycles[0]) == {"a", "b"}
    assert "a" not in graph.order
    assert "b" not in graph.order


def test_build_dependency_graph_detects_three_node_cycle():
    a = _manifest("a", dependencies=[PluginDependency(plugin_id="c")])
    b = _manifest("b", dependencies=[PluginDependency(plugin_id="a")])
    c = _manifest("c", dependencies=[PluginDependency(plugin_id="b")])
    graph = plugin_validator.build_dependency_graph([a, b, c])
    assert len(graph.cycles) == 1
    assert set(graph.cycles[0]) == {"a", "b", "c"}


def test_build_dependency_graph_deterministic_across_orderings():
    a = _manifest("a")
    b = _manifest("b", dependencies=[PluginDependency(plugin_id="a")])
    graph1 = plugin_validator.build_dependency_graph([a, b])
    graph2 = plugin_validator.build_dependency_graph([b, a])
    assert graph1.order == graph2.order == ["a", "b"]


def test_validate_all_folds_duplicate_and_cyclic_and_missing_into_results():
    a = _manifest("a", dependencies=[PluginDependency(plugin_id="b")])
    b = _manifest("b", dependencies=[PluginDependency(plugin_id="a")])
    ghost_dep = _manifest("g", dependencies=[PluginDependency(plugin_id="nonexistent")])
    results = plugin_validator.validate_all([a, b, ghost_dep])
    assert not results["a"].valid
    assert not results["b"].valid
    assert any(i.rule_id == "PLUGIN-CYCLIC-DEPENDENCY" for i in results["a"].issues)
    assert any(i.rule_id == "PLUGIN-MISSING-DEPENDENCY" for i in results["g"].issues)


def test_validate_all_valid_plugin_has_no_issues():
    a = _manifest("solo")
    results = plugin_validator.validate_all([a])
    assert results["solo"].valid
    assert results["solo"].issues == []


# ══════════════════════════════════════════════════════════════
# LAYER 4 — Loader
# ══════════════════════════════════════════════════════════════

def test_discover_plugins_python_package():
    with tempfile.TemporaryDirectory() as tmp:
        _write_plugin_package(tmp, "pkg-plugin")
        discovered = plugin_loader.discover_plugins(tmp)
        assert len(discovered) == 1
        assert discovered[0].manifest.id == "pkg-plugin"
        assert discovered[0].source_type == PluginSourceType.PYTHON_PACKAGE


def test_discover_plugins_zip_bundle():
    with tempfile.TemporaryDirectory() as tmp:
        _write_plugin_zip(tmp, "zip-plugin")
        discovered = plugin_loader.discover_plugins(tmp)
        assert len(discovered) == 1
        assert discovered[0].source_type == PluginSourceType.ZIP_BUNDLE


def test_discover_plugins_editable_path():
    with tempfile.TemporaryDirectory() as editable_root, tempfile.TemporaryDirectory() as plugins_root:
        plugin_dir = _write_plugin_package(editable_root, "editable-plugin")
        discovered = plugin_loader.discover_plugins(plugins_root, editable_paths=[plugin_dir])
        assert len(discovered) == 1
        assert discovered[0].source_type == PluginSourceType.EDITABLE


def test_discover_plugins_skips_directory_without_manifest():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "not-a-plugin"))
        discovered = plugin_loader.discover_plugins(tmp)
        assert discovered == []


def test_discover_plugins_nonexistent_dir_returns_empty():
    assert plugin_loader.discover_plugins("/no/such/directory") == []


def test_discover_plugins_sorted_by_id():
    with tempfile.TemporaryDirectory() as tmp:
        _write_plugin_package(tmp, "zzz-plugin")
        _write_plugin_package(tmp, "aaa-plugin")
        discovered = plugin_loader.discover_plugins(tmp)
        assert [d.manifest.id for d in discovered] == ["aaa-plugin", "zzz-plugin"]


def test_import_entrypoint_python_package():
    with tempfile.TemporaryDirectory() as tmp:
        _write_plugin_package(tmp, "import-test")
        discovered = plugin_loader.discover_plugins(tmp)
        module = plugin_loader.import_entrypoint(discovered[0])
        assert hasattr(module, "before_workflow")
        assert hasattr(module, "after_workflow")


def test_import_entrypoint_zip_bundle_extracts_and_cleans_up():
    with tempfile.TemporaryDirectory() as tmp:
        _write_plugin_zip(tmp, "zip-import")
        discovered = plugin_loader.discover_plugins(tmp)
        extracted: Dict[str, str] = {}
        module = plugin_loader.import_entrypoint(discovered[0], _extracted_dirs=extracted)
        assert hasattr(module, "before_workflow")
        assert "zip-import" in extracted
        temp_dir = extracted["zip-import"]
        assert os.path.isdir(temp_dir)
        plugin_loader.cleanup_extracted(extracted, "zip-import")
        assert not os.path.isdir(temp_dir)
        assert "zip-import" not in extracted


def test_import_entrypoint_missing_entrypoint_raises():
    with tempfile.TemporaryDirectory() as tmp:
        plugin_dir = _write_plugin_package(tmp, "broken-entry", entrypoint="does_not_exist.py")
        discovered = plugin_loader.discover_plugins(tmp)
        with pytest.raises(plugin_loader.PluginLoadError):
            plugin_loader.import_entrypoint(discovered[0])


def test_import_entrypoint_raising_module_raises_plugin_load_error():
    with tempfile.TemporaryDirectory() as tmp:
        _write_plugin_package(tmp, "raises-on-import", entrypoint_code="raise ValueError('boom')\n")
        discovered = plugin_loader.discover_plugins(tmp)
        with pytest.raises(plugin_loader.PluginLoadError):
            plugin_loader.import_entrypoint(discovered[0])


def test_validate_entrypoint_importable_success_and_failure():
    with tempfile.TemporaryDirectory() as tmp:
        _write_plugin_package(tmp, "good-import")
        _write_plugin_package(tmp, "bad-import", entrypoint_code="raise RuntimeError('nope')\n")
        discovered = plugin_loader.discover_plugins(tmp)
        good = next(d for d in discovered if d.manifest.id == "good-import")
        bad = next(d for d in discovered if d.manifest.id == "bad-import")
        assert plugin_loader.validate_entrypoint_importable(good) is None
        assert plugin_loader.validate_entrypoint_importable(bad) is not None


def test_two_plugins_with_same_entrypoint_filename_do_not_collide():
    with tempfile.TemporaryDirectory() as tmp:
        _write_plugin_package(tmp, "plugin-one", entrypoint_code="def before_workflow(ctx, **kw):\n    return 1\n")
        _write_plugin_package(tmp, "plugin-two", entrypoint_code="def before_workflow(ctx, **kw):\n    return 2\n")
        discovered = plugin_loader.discover_plugins(tmp)
        mod1 = plugin_loader.import_entrypoint(next(d for d in discovered if d.manifest.id == "plugin-one"))
        mod2 = plugin_loader.import_entrypoint(next(d for d in discovered if d.manifest.id == "plugin-two"))
        assert mod1.before_workflow(None) == 1
        assert mod2.before_workflow(None) == 2


def test_bad_zip_file_raises_plugin_load_error():
    with tempfile.TemporaryDirectory() as tmp:
        bad_zip = os.path.join(tmp, "corrupt.zip")
        with open(bad_zip, "w") as fh:
            fh.write("not a real zip")
        discovered = plugin_loader.discover_plugins(tmp)
        assert discovered == []  # discovery skips it rather than raising


# ══════════════════════════════════════════════════════════════
# LAYER 5 — Registry
# ══════════════════════════════════════════════════════════════

def test_registry_install_and_get():
    registry = PluginRegistry()
    manifest = _manifest("reg-1")
    record = registry.install(manifest, PluginSourceType.PYTHON_PACKAGE, "/tmp/reg-1")
    assert record.state == PluginState.INSTALLED
    assert registry.get("reg-1").manifest.id == "reg-1"


def test_registry_install_duplicate_raises():
    registry = PluginRegistry()
    manifest = _manifest("dup")
    registry.install(manifest, PluginSourceType.PYTHON_PACKAGE)
    with pytest.raises(PluginAlreadyInstalledError):
        registry.install(manifest, PluginSourceType.PYTHON_PACKAGE)


def test_registry_enable_disable_lifecycle():
    registry = PluginRegistry()
    registry.install(_manifest("lifecycle"), PluginSourceType.PYTHON_PACKAGE)
    registry.enable("lifecycle")
    assert registry.get("lifecycle").state == PluginState.ENABLED
    registry.disable("lifecycle")
    assert registry.get("lifecycle").state == PluginState.DISABLED
    assert registry.get("lifecycle").disabled_at is not None


def test_registry_enable_nonexistent_raises():
    registry = PluginRegistry()
    with pytest.raises(PluginNotFoundError):
        registry.enable("nope")


def test_registry_remove():
    registry = PluginRegistry()
    registry.install(_manifest("removable"), PluginSourceType.PYTHON_PACKAGE)
    registry.remove("removable")
    assert registry.get("removable") is None
    with pytest.raises(PluginNotFoundError):
        registry.remove("removable")


def test_registry_mark_error():
    registry = PluginRegistry()
    registry.install(_manifest("erroring"), PluginSourceType.PYTHON_PACKAGE)
    record = registry.mark_error("erroring", "boom")
    assert record.state == PluginState.ERROR
    assert record.last_error == "boom"


def test_registry_list_views():
    registry = PluginRegistry()
    registry.install(_manifest("e1"), PluginSourceType.PYTHON_PACKAGE, enabled_by_default=True)
    registry.install(_manifest("d1"), PluginSourceType.PYTHON_PACKAGE)
    registry.disable("d1")
    assert [r.manifest.id for r in registry.list_enabled()] == ["e1"]
    assert [r.manifest.id for r in registry.list_disabled()] == ["d1"]
    assert len(registry.list_installed()) == 2


def test_registry_list_hooks_and_permissions():
    registry = PluginRegistry()
    registry.install(_manifest("h1", hooks=["before_workflow"], permissions=["read"]),
                      PluginSourceType.PYTHON_PACKAGE, enabled_by_default=True)
    registry.install(_manifest("h2", hooks=["before_workflow", "after_workflow"], permissions=["write"]),
                      PluginSourceType.PYTHON_PACKAGE, enabled_by_default=True)
    hooks = registry.list_hooks()
    assert hooks["before_workflow"] == ["h1", "h2"]
    assert hooks["after_workflow"] == ["h2"]
    perms = registry.list_permissions()
    assert perms["read"] == ["h1"]


def test_registry_list_hooks_enabled_only_excludes_disabled():
    registry = PluginRegistry()
    registry.install(_manifest("disabled-hook", hooks=["startup"]), PluginSourceType.PYTHON_PACKAGE)
    assert registry.list_hooks(enabled_only=True) == {}
    assert registry.list_hooks(enabled_only=False) == {"startup": ["disabled-hook"]}


def test_registry_list_capabilities():
    registry = PluginRegistry()
    registry.install(_manifest("cap-1", hooks=["startup"], permissions=["read"]),
                      PluginSourceType.PYTHON_PACKAGE, enabled_by_default=True)
    caps = registry.list_capabilities()
    assert len(caps) == 1
    assert caps[0].plugin_id == "cap-1"
    assert caps[0].hooks == ["startup"]


def test_registry_has_permission():
    registry = PluginRegistry()
    registry.install(_manifest("perm-check", permissions=["network"]), PluginSourceType.PYTHON_PACKAGE)
    assert registry.has_permission("perm-check", "network") is True
    assert registry.has_permission("perm-check", "shell") is False
    assert registry.has_permission("missing", "network") is False


# ══════════════════════════════════════════════════════════════
# LAYER 6 — Runtime
# ══════════════════════════════════════════════════════════════

def test_context_for_plugin_scopes_by_permission():
    base = PluginContext(logger="log", repository="repo", artifact_manager="artifacts",
                          event_publisher=lambda *a: None, runtime="rt")
    scoped_full = context_for_plugin(base, ["read", "events", "artifacts"])
    assert scoped_full.repository == "repo"
    assert scoped_full.artifact_manager == "artifacts"
    assert scoped_full.event_publisher is not None

    scoped_none = context_for_plugin(base, [])
    assert scoped_none.repository is None
    assert scoped_none.artifact_manager is None
    assert scoped_none.event_publisher is None
    assert scoped_none.logger == "log"  # always present


def test_context_for_plugin_write_permission_grants_repository():
    base = PluginContext(repository="repo")
    scoped = context_for_plugin(base, ["write"])
    assert scoped.repository == "repo"


class _FakeModule:
    """Stand-in for an imported plugin module — see
    `plugin_runtime.dispatch_hook`'s docstring on why this function
    doesn't need real `plugin_loader` imports to test."""
    def __init__(self, fn, hook_name: str = "before_workflow"):
        if fn is not None:
            setattr(self, hook_name, fn)


def test_dispatch_hook_success_and_failure_isolation():
    def ok_fn(ctx, **kw):
        return "ok"

    def boom_fn(ctx, **kw):
        raise RuntimeError("plugin exploded")

    record_ok = PluginRecord(manifest=_manifest("ok-plugin", hooks=["before_workflow"]), state=PluginState.ENABLED)
    record_bad = PluginRecord(manifest=_manifest("bad-plugin", hooks=["before_workflow"]), state=PluginState.ENABLED)
    modules = {"ok-plugin": _FakeModule(ok_fn), "bad-plugin": _FakeModule(boom_fn)}

    results = dispatch_hook(HookType.BEFORE_WORKFLOW, [record_ok, record_bad], modules, PluginContext())
    by_id = {r.plugin_id: r for r in results}
    assert by_id["ok-plugin"].success is True
    assert by_id["ok-plugin"].result == "ok"
    assert by_id["bad-plugin"].success is False
    assert "plugin exploded" in by_id["bad-plugin"].error


def test_dispatch_hook_skips_plugins_without_that_hook():
    record = PluginRecord(manifest=_manifest("no-hook-here", hooks=[]), state=PluginState.ENABLED)
    modules = {"no-hook-here": _FakeModule(None)}
    results = dispatch_hook(HookType.STARTUP, [record], modules, PluginContext())
    assert results == []


def test_dispatch_hook_preserves_caller_supplied_order():
    calls = []

    def make_fn(name):
        def fn(ctx, **kw):
            calls.append(name)
            return name
        return fn

    record_z = PluginRecord(manifest=_manifest("z-plugin", hooks=["startup"]), state=PluginState.ENABLED)
    record_a = PluginRecord(manifest=_manifest("a-plugin", hooks=["startup"]), state=PluginState.ENABLED)
    modules = {"z-plugin": _FakeModule(make_fn("z"), "startup"), "a-plugin": _FakeModule(make_fn("a"), "startup")}
    dispatch_hook(HookType.STARTUP, [record_z, record_a], modules, PluginContext())
    assert calls == ["z", "a"]  # order as given, not re-sorted


def test_dispatch_hook_scopes_context_per_plugin_permissions():
    def fn(ctx, **kw):
        return ctx.repository

    record = PluginRecord(manifest=_manifest("scoped", hooks=["startup"], permissions=[]), state=PluginState.ENABLED)
    modules = {"scoped": _FakeModule(fn, "startup")}
    base_ctx = PluginContext(repository="secret-repo")
    results = dispatch_hook(HookType.STARTUP, [record], modules, base_ctx)
    assert results[0].result is None  # no "read"/"write" permission declared


def test_compute_health_no_executions_is_healthy():
    health = compute_health("p1", PluginState.ENABLED, [])
    assert health.healthy is True
    assert health.success_rate == 1.0


def test_compute_health_all_failures_is_unhealthy():
    from services.integration.plugin_sdk.plugin_models import HookExecutionResult
    executions = [HookExecutionResult(plugin_id="p1", hook=HookType.STARTUP, success=False, error="e",
                                       executed_at="2026-01-01T00:00:00+00:00")]
    health = compute_health("p1", PluginState.ENABLED, executions)
    assert health.healthy is False
    assert health.failed_executions == 1
    assert health.last_error == "e"


def test_compute_health_error_state_always_unhealthy():
    from services.integration.plugin_sdk.plugin_models import HookExecutionResult
    executions = [HookExecutionResult(plugin_id="p1", hook=HookType.STARTUP, success=True,
                                       executed_at="2026-01-01T00:00:00+00:00")]
    health = compute_health("p1", PluginState.ERROR, executions)
    assert health.healthy is False


# ══════════════════════════════════════════════════════════════
# LAYER 7 — Repository
# ══════════════════════════════════════════════════════════════

@pytest.fixture
async def db_session():
    from infrastructure.database.plugin_sdk_models import Base

    engine = create_async_engine("sqlite+aiosqlite://", connect_args={"check_same_thread": False},
                                  poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_repository_record_and_get_plugin(db_session):
    from services.integration.plugin_sdk.plugin_repository import PluginRepository

    manifest = _manifest("repo-plugin", hooks=["startup"], permissions=["read"])
    await PluginRepository.record_plugin(db_session, manifest, PluginSourceType.PYTHON_PACKAGE, "/plugins/repo-plugin")
    await db_session.commit()

    record = await PluginRepository.get_plugin(db_session, "repo-plugin")
    assert record is not None
    assert record.manifest.hooks == ["startup"]
    assert record.state == PluginState.INSTALLED


@pytest.mark.asyncio
async def test_repository_set_state_appends_history(db_session):
    from services.integration.plugin_sdk.plugin_repository import PluginRepository

    manifest = _manifest("state-plugin")
    await PluginRepository.record_plugin(db_session, manifest, PluginSourceType.PYTHON_PACKAGE)
    await db_session.commit()
    await PluginRepository.set_state(db_session, "state-plugin", PluginState.ENABLED)
    await db_session.commit()

    record = await PluginRepository.get_plugin(db_session, "state-plugin")
    assert record.state == PluginState.ENABLED
    assert record.enabled_at is not None


@pytest.mark.asyncio
async def test_repository_list_plugins(db_session):
    from services.integration.plugin_sdk.plugin_repository import PluginRepository

    for pid in ("list-a", "list-b"):
        await PluginRepository.record_plugin(db_session, _manifest(pid), PluginSourceType.PYTHON_PACKAGE)
        await db_session.commit()

    records = await PluginRepository.list_plugins(db_session)
    assert [r.manifest.id for r in records] == ["list-a", "list-b"]


@pytest.mark.asyncio
async def test_repository_record_and_list_executions(db_session):
    from services.integration.plugin_sdk.plugin_models import PluginExecutionResult
    from services.integration.plugin_sdk.plugin_repository import PluginRepository

    await PluginRepository.record_plugin(db_session, _manifest("exec-plugin"), PluginSourceType.PYTHON_PACKAGE)
    await db_session.commit()
    execution = PluginExecutionResult(plugin_id="exec-plugin", action="capability:foo", success=True,
                                       duration_ms=1.5, executed_at="2026-01-01T00:00:00+00:00")
    await PluginRepository.record_execution(db_session, execution)
    await db_session.commit()

    executions = await PluginRepository.list_executions(db_session, "exec-plugin")
    assert len(executions) == 1
    assert executions[0].action == "capability:foo"


@pytest.mark.asyncio
async def test_repository_record_and_list_hook_executions(db_session):
    from services.integration.plugin_sdk.plugin_models import HookExecutionResult
    from services.integration.plugin_sdk.plugin_repository import PluginRepository

    await PluginRepository.record_plugin(db_session, _manifest("hook-exec-plugin"), PluginSourceType.PYTHON_PACKAGE)
    await db_session.commit()
    result = HookExecutionResult(plugin_id="hook-exec-plugin", hook=HookType.STARTUP, success=False, error="e",
                                  executed_at="2026-01-01T00:00:00+00:00")
    await PluginRepository.record_hook_execution(db_session, result)
    await db_session.commit()

    hook_executions = await PluginRepository.list_hook_executions(db_session, "hook-exec-plugin")
    assert len(hook_executions) == 1
    assert hook_executions[0].hook == HookType.STARTUP
    assert hook_executions[0].success is False


@pytest.mark.asyncio
async def test_repository_get_plugin_missing_returns_none(db_session):
    from services.integration.plugin_sdk.plugin_repository import PluginRepository
    assert await PluginRepository.get_plugin(db_session, "nonexistent") is None


# ══════════════════════════════════════════════════════════════
# LAYER 8 — Export
# ══════════════════════════════════════════════════════════════

def _sample_records() -> List[PluginRecord]:
    return [
        PluginRecord(manifest=_manifest("exp-1"), state=PluginState.ENABLED, installed_at="2026-01-01T00:00:00+00:00"),
        PluginRecord(manifest=_manifest("exp-2"), state=PluginState.DISABLED, installed_at="2026-01-01T00:00:00+00:00"),
    ]


def test_export_inventory_all_formats():
    records = _sample_records()
    assert json.loads(plugin_export.export_inventory(records, "json"))[0]["manifest"]["id"] == "exp-1"
    assert "exp-1" in plugin_export.export_inventory(records, "markdown")
    assert "<table" in plugin_export.export_inventory(records, "html")


def test_export_inventory_unknown_format_raises():
    with pytest.raises(ValueError):
        plugin_export.export_inventory(_sample_records(), "xml")


def test_export_health_all_formats():
    health = [compute_health("exp-1", PluginState.ENABLED, [])]
    assert json.loads(plugin_export.export_health(health, "json"))[0]["plugin_id"] == "exp-1"
    assert "exp-1" in plugin_export.export_health(health, "markdown")
    assert "<table" in plugin_export.export_health(health, "html")


def test_export_validation_all_formats():
    results = plugin_validator.validate_all([_manifest("v1", permissions=["bogus"])])
    assert "v1" in json.loads(plugin_export.export_validation(results, "json"))
    md = plugin_export.export_validation(results, "markdown")
    assert "v1" in md and "invalid" in md
    assert "<h2>" in plugin_export.export_validation(results, "html")


# ══════════════════════════════════════════════════════════════
# LAYER 9 — Dashboard
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_fetch_plugin_dashboard_section_no_db_returns_empty_shape():
    from services.integration.plugin_sdk.plugin_repository import fetch_plugin_dashboard_section
    section = await fetch_plugin_dashboard_section(db=None)
    assert section["installed_count"] == 0
    assert section["unhealthy_plugins"] == []


@pytest.mark.asyncio
async def test_fetch_plugin_dashboard_section_with_db(db_session):
    from services.integration.plugin_sdk.plugin_repository import PluginRepository, fetch_plugin_dashboard_section

    await PluginRepository.record_plugin(db_session, _manifest("dash-p1"), PluginSourceType.PYTHON_PACKAGE,
                                          state=PluginState.ENABLED)
    await db_session.commit()

    section = await fetch_plugin_dashboard_section(db_session)
    assert section is not None
    assert section["installed_count"] == 1
    assert section["enabled_count"] == 1


def test_build_plugins_summary_none_when_raw_is_none():
    from services.integration.dashboard.dashboard_builder import build_plugins_summary
    assert build_plugins_summary(None) is None


def test_build_plugins_summary_from_raw():
    from services.integration.dashboard.dashboard_builder import build_plugins_summary
    summary = build_plugins_summary({"installed_count": 3, "enabled_count": 2, "disabled_count": 1,
                                       "error_count": 0, "unhealthy_plugins": ["p1"]})
    assert summary.installed_count == 3
    assert summary.unhealthy_plugins == ["p1"]


def test_platform_dashboard_plugins_field_wired():
    from services.integration.dashboard.dashboard_builder import assemble_platform_dashboard, build_plugins_summary
    from services.integration.dashboard.dashboard_models import MetricsSnapshot

    plugins_summary = build_plugins_summary({"installed_count": 1, "enabled_count": 1, "disabled_count": 0,
                                              "error_count": 0, "unhealthy_plugins": []})
    dashboard = assemble_platform_dashboard(
        services=[], workflows=[], events=[], incidents=[], versions=[],
        metrics=MetricsSnapshot(available=False), plugins=plugins_summary,
    )
    assert dashboard.plugins is not None
    assert dashboard.plugins.installed_count == 1


# ══════════════════════════════════════════════════════════════
# LAYER 10 — API
# ══════════════════════════════════════════════════════════════

@pytest.fixture
def plugins_dir_fixture():
    with tempfile.TemporaryDirectory() as tmp:
        _write_plugin_package(tmp, "api-plugin")
        yield tmp


@pytest.fixture
def app_no_db(plugins_dir_fixture):
    from fastapi import FastAPI
    from services.integration.api.plugins import router
    app = FastAPI()
    app.include_router(router)
    app.state.db_factory = None
    app.state.plugins_dir = plugins_dir_fixture
    return app


@pytest.fixture
def app_with_db(plugins_dir_fixture):
    from fastapi import FastAPI
    from infrastructure.database.plugin_sdk_models import Base
    from services.integration.api.plugins import router

    app = FastAPI()
    app.include_router(router)
    app.state.plugins_dir = plugins_dir_fixture

    engine = create_async_engine("sqlite+aiosqlite://", connect_args={"check_same_thread": False},
                                  poolclass=StaticPool)

    @asynccontextmanager
    async def db_factory():
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
            await session.commit()

    app.state.db_factory = db_factory
    app.state._engine = engine
    return app


@pytest.fixture
async def _init_schema(app_with_db):
    from infrastructure.database.plugin_sdk_models import Base
    async with app_with_db.state._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return app_with_db


def _client(app):
    from httpx import ASGITransport, AsyncClient
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_api_list_plugins_empty(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.get("/plugins")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0


@pytest.mark.asyncio
async def test_api_install_then_list(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.post("/plugins/install", json={"plugin_id": "api-plugin"})
        assert resp.status_code == 200
        assert resp.json()["plugin"]["manifest"]["id"] == "api-plugin"

        list_resp = await client.get("/plugins")
        assert list_resp.json()["count"] == 1


@pytest.mark.asyncio
async def test_api_install_unknown_plugin_404(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.post("/plugins/install", json={"plugin_id": "does-not-exist"})
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_api_install_missing_plugin_id_422(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.post("/plugins/install", json={})
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_api_install_twice_conflicts(app_no_db):
    async with _client(app_no_db) as client:
        await client.post("/plugins/install", json={"plugin_id": "api-plugin"})
        resp = await client.post("/plugins/install", json={"plugin_id": "api-plugin"})
        assert resp.status_code == 409


@pytest.mark.asyncio
async def test_api_get_plugin_by_id(app_no_db):
    async with _client(app_no_db) as client:
        await client.post("/plugins/install", json={"plugin_id": "api-plugin"})
        resp = await client.get("/plugins/api-plugin")
        assert resp.status_code == 200
        assert resp.json()["manifest"]["id"] == "api-plugin"


@pytest.mark.asyncio
async def test_api_get_plugin_404_when_absent(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.get("/plugins/nope")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_api_enable_disable(app_no_db):
    async with _client(app_no_db) as client:
        await client.post("/plugins/install", json={"plugin_id": "api-plugin"})
        enable_resp = await client.post("/plugins/enable", json={"plugin_id": "api-plugin"})
        assert enable_resp.json()["plugin"]["state"] == "enabled"
        disable_resp = await client.post("/plugins/disable", json={"plugin_id": "api-plugin"})
        assert disable_resp.json()["plugin"]["state"] == "disabled"


@pytest.mark.asyncio
async def test_api_enable_nonexistent_404(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.post("/plugins/enable", json={"plugin_id": "ghost"})
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_api_reload_success(app_no_db):
    async with _client(app_no_db) as client:
        await client.post("/plugins/install", json={"plugin_id": "api-plugin"})
        resp = await client.post("/plugins/reload", json={"plugin_id": "api-plugin"})
        assert resp.status_code == 200
        assert resp.json()["reloaded"] is True


@pytest.mark.asyncio
async def test_api_reload_broken_plugin_marks_error(app_no_db, plugins_dir_fixture):
    _write_plugin_package(plugins_dir_fixture, "broken-plugin", entrypoint_code="raise RuntimeError('x')\n")
    async with _client(app_no_db) as client:
        await client.post("/plugins/install", json={"plugin_id": "broken-plugin"})
        resp = await client.post("/plugins/reload", json={"plugin_id": "broken-plugin"})
        assert resp.status_code == 200
        assert resp.json()["reloaded"] is False
        assert resp.json()["plugin"]["state"] == "error"


@pytest.mark.asyncio
async def test_api_delete_plugin(app_no_db):
    async with _client(app_no_db) as client:
        await client.post("/plugins/install", json={"plugin_id": "api-plugin"})
        resp = await client.delete("/plugins/api-plugin")
        assert resp.status_code == 200
        assert resp.json()["removed"] == "api-plugin"
        list_resp = await client.get("/plugins")
        assert list_resp.json()["count"] == 0


@pytest.mark.asyncio
async def test_api_delete_nonexistent_404(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.delete("/plugins/ghost")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_api_hooks_and_capabilities(app_no_db):
    async with _client(app_no_db) as client:
        await client.post("/plugins/install", json={"plugin_id": "api-plugin"})
        await client.post("/plugins/enable", json={"plugin_id": "api-plugin"})
        hooks_resp = await client.get("/plugins/hooks")
        assert "before_workflow" in hooks_resp.json()
        caps_resp = await client.get("/plugins/capabilities")
        assert caps_resp.json()["capabilities"][0]["plugin_id"] == "api-plugin"


@pytest.mark.asyncio
async def test_api_health(app_no_db):
    async with _client(app_no_db) as client:
        await client.post("/plugins/install", json={"plugin_id": "api-plugin"})
        resp = await client.get("/plugins/health")
        assert resp.status_code == 200
        assert resp.json()["health"][0]["plugin_id"] == "api-plugin"
        assert resp.json()["health"][0]["healthy"] is True


@pytest.mark.asyncio
async def test_api_persists_to_db_when_configured(_init_schema):
    app = _init_schema
    async with _client(app) as client:
        install_resp = await client.post("/plugins/install", json={"plugin_id": "api-plugin"})
        assert install_resp.json()["persisted"] is True

        list_resp = await client.get("/plugins")
        assert list_resp.json()["source"] == "database"
        assert list_resp.json()["count"] == 1


# ══════════════════════════════════════════════════════════════
# LAYER 11 — CLI
# ══════════════════════════════════════════════════════════════

def test_cli_list(capsys):
    from services.integration.plugin_sdk import plugin_cli
    with tempfile.TemporaryDirectory() as tmp:
        _write_plugin_package(tmp, "cli-plugin")
        rc = plugin_cli.main(["--plugins-dir", tmp, "plugin", "list"])
        assert rc == 0
        assert "cli-plugin" in capsys.readouterr().out


def test_cli_list_empty(capsys):
    from services.integration.plugin_sdk import plugin_cli
    with tempfile.TemporaryDirectory() as tmp:
        rc = plugin_cli.main(["--plugins-dir", tmp, "plugin", "list"])
        assert rc == 0
        assert "No plugins found." in capsys.readouterr().out


def test_cli_validate_valid_plugin(capsys):
    from services.integration.plugin_sdk import plugin_cli
    with tempfile.TemporaryDirectory() as tmp:
        _write_plugin_package(tmp, "valid-plugin")
        rc = plugin_cli.main(["--plugins-dir", tmp, "plugin", "validate"])
        assert rc == 0
        assert "valid-plugin" in capsys.readouterr().out


def test_cli_validate_invalid_plugin_nonzero_exit(capsys):
    from services.integration.plugin_sdk import plugin_cli
    with tempfile.TemporaryDirectory() as tmp:
        _write_plugin_package(tmp, "invalid-plugin", permissions=["bogus-permission"])
        rc = plugin_cli.main(["--plugins-dir", tmp, "plugin", "validate"])
        assert rc == 1


def test_cli_install_without_db_url(capsys):
    from services.integration.plugin_sdk import plugin_cli
    with tempfile.TemporaryDirectory() as tmp:
        _write_plugin_package(tmp, "install-me")
        rc = plugin_cli.main(["--plugins-dir", tmp, "plugin", "install", "install-me"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Installed 'install-me'" in out
        assert "not durably recorded" in out


def test_cli_install_unknown_plugin(capsys):
    from services.integration.plugin_sdk import plugin_cli
    with tempfile.TemporaryDirectory() as tmp:
        rc = plugin_cli.main(["--plugins-dir", tmp, "plugin", "install", "ghost"])
        assert rc == 1


def test_cli_install_invalid_manifest_refused(capsys):
    from services.integration.plugin_sdk import plugin_cli
    with tempfile.TemporaryDirectory() as tmp:
        _write_plugin_package(tmp, "bad-perm-plugin", permissions=["not-a-real-permission"])
        rc = plugin_cli.main(["--plugins-dir", tmp, "plugin", "install", "bad-perm-plugin"])
        assert rc == 1
        assert "Refusing to install" in capsys.readouterr().out


def test_cli_enable_disable_without_db_url(capsys):
    from services.integration.plugin_sdk import plugin_cli
    rc = plugin_cli.main(["plugin", "enable", "some-plugin"])
    assert rc == 0
    assert "Would enable" in capsys.readouterr().out


def test_cli_reload_success(capsys):
    from services.integration.plugin_sdk import plugin_cli
    with tempfile.TemporaryDirectory() as tmp:
        _write_plugin_package(tmp, "reload-me")
        rc = plugin_cli.main(["--plugins-dir", tmp, "plugin", "reload", "reload-me"])
        assert rc == 0
        assert "Reloaded 'reload-me'" in capsys.readouterr().out


def test_cli_reload_broken_plugin_fails(capsys):
    from services.integration.plugin_sdk import plugin_cli
    with tempfile.TemporaryDirectory() as tmp:
        _write_plugin_package(tmp, "reload-broken", entrypoint_code="raise RuntimeError('boom')\n")
        rc = plugin_cli.main(["--plugins-dir", tmp, "plugin", "reload", "reload-broken"])
        assert rc == 1
        assert "Reload failed" in capsys.readouterr().out


def test_cli_remove_without_db_url(capsys):
    from services.integration.plugin_sdk import plugin_cli
    rc = plugin_cli.main(["plugin", "remove", "some-plugin"])
    assert rc == 0
    assert "Removed 'some-plugin'" in capsys.readouterr().out


def test_cli_install_and_persist_with_db_url(capsys, tmp_path):
    from services.integration.plugin_sdk import plugin_cli
    db_path = tmp_path / "plugins.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"

    with tempfile.TemporaryDirectory() as tmp:
        _write_plugin_package(tmp, "persisted-plugin")

        async def _create_schema():
            from infrastructure.database.plugin_sdk_models import Base
            engine = create_async_engine(db_url)
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            await engine.dispose()

        import asyncio
        asyncio.run(_create_schema())

        rc = plugin_cli.main(["--plugins-dir", tmp, "--db-url", db_url, "plugin", "install", "persisted-plugin"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "not durably recorded" not in out


# ══════════════════════════════════════════════════════════════
# LAYER 12 — Regression / isolation
# ══════════════════════════════════════════════════════════════

def test_package_modules_import_independently():
    import importlib
    for module_name in (
        "services.integration.plugin_sdk.plugin_models",
        "services.integration.plugin_sdk.plugin_manifest",
        "services.integration.plugin_sdk.plugin_loader",
        "services.integration.plugin_sdk.plugin_validator",
        "services.integration.plugin_sdk.plugin_registry",
        "services.integration.plugin_sdk.plugin_runtime",
        "services.integration.plugin_sdk.plugin_repository",
        "services.integration.plugin_sdk.plugin_export",
        "services.integration.plugin_sdk.plugin_cli",
    ):
        importlib.import_module(module_name)


def test_two_independent_registries_do_not_share_state():
    r1 = PluginRegistry()
    r2 = PluginRegistry()
    r1.install(_manifest("only-in-r1"), PluginSourceType.PYTHON_PACKAGE)
    assert r2.get("only-in-r1") is None
    assert len(r1.list_all()) == 1
    assert len(r2.list_all()) == 0


# ══════════════════════════════════════════════════════════════
# LAYER 13 — Additional edge-case coverage
# ══════════════════════════════════════════════════════════════

# --- Validator edge cases -------------------------------------------------

def test_build_dependency_graph_diamond_shape_orders_correctly():
    # a <- b, a <- c, b <- d, c <- d  (d depends on b and c, both depend on a)
    a = _manifest("a")
    b = _manifest("b", dependencies=[PluginDependency(plugin_id="a")])
    c = _manifest("c", dependencies=[PluginDependency(plugin_id="a")])
    d = _manifest("d", dependencies=[PluginDependency(plugin_id="b"), PluginDependency(plugin_id="c")])
    graph = plugin_validator.build_dependency_graph([d, c, b, a])
    assert graph.order.index("a") < graph.order.index("b")
    assert graph.order.index("a") < graph.order.index("c")
    assert graph.order.index("b") < graph.order.index("d")
    assert graph.order.index("c") < graph.order.index("d")
    assert graph.cycles == []


def test_build_dependency_graph_self_dependency_is_a_cycle():
    a = _manifest("self-dep", dependencies=[PluginDependency(plugin_id="self-dep")])
    graph = plugin_validator.build_dependency_graph([a])
    assert graph.cycles
    assert "self-dep" not in graph.order


def test_build_dependency_graph_empty_input():
    graph = plugin_validator.build_dependency_graph([])
    assert graph.order == []
    assert graph.cycles == []
    assert graph.missing_dependencies == {}


def test_validate_version_compatibility_default_platform_version_matches_manifest_default():
    m = _manifest("default-version")  # api_version defaults to "1.0.0"
    assert plugin_validator.validate_version_compatibility(m) == []


def test_validate_permissions_empty_list_is_fine():
    m = _manifest(permissions=[])
    assert plugin_validator.validate_permissions(m) == []


def test_validate_hooks_empty_list_is_fine():
    m = _manifest(hooks=[])
    assert plugin_validator.validate_hooks(m) == []


def test_validate_duplicate_ids_no_duplicates_returns_empty():
    manifests = [_manifest("x"), _manifest("y"), _manifest("z")]
    assert plugin_validator.validate_duplicate_ids(manifests) == {}


def test_parse_version_helper_handles_nonnumeric_suffix():
    assert plugin_validator._parse_version("1.2.3-beta") == (1, 2, 3)


def test_is_compatible_helper_major_zero_matches_major_zero():
    assert plugin_validator._is_compatible("0.9.0", "0.5.0") is True
    assert plugin_validator._is_compatible("0.9.0", "1.0.0") is False


# --- Manifest edge cases ---------------------------------------------------

def test_manifest_error_message_lists_all_missing_fields():
    with pytest.raises(plugin_manifest.ManifestError, match="name.*author|author.*name"):
        plugin_manifest.parse_manifest({"id": "x", "entrypoint": "plugin.py", "version": "1.0.0"})


def test_load_manifest_json_with_dependencies_and_hooks():
    data = _manifest_dict(dependencies=[{"plugin_id": "other", "version_constraint": ">=1.0"}])
    m = plugin_manifest.load_manifest_json(json.dumps(data))
    assert m.dependencies[0].plugin_id == "other"
    assert m.dependencies[0].version_constraint == ">=1.0"


def test_manifest_filenames_constant_contains_expected_names():
    assert set(plugin_manifest.MANIFEST_FILENAMES) == {"manifest.json", "plugin.json", "manifest.toml"}


# --- Loader edge cases -----------------------------------------------------

def test_discover_plugins_finds_both_directory_and_zip_sources():
    with tempfile.TemporaryDirectory() as tmp:
        _write_plugin_package(tmp, "dir-source")
        _write_plugin_zip(tmp, "zip-source")
        discovered = plugin_loader.discover_plugins(tmp)
        ids = {d.manifest.id for d in discovered}
        assert ids == {"dir-source", "zip-source"}


def test_discover_plugins_skips_zip_with_no_manifest():
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, "no-manifest.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("plugin.py", "x = 1\n")
        discovered = plugin_loader.discover_plugins(tmp)
        assert discovered == []


def test_discover_plugins_skips_directory_with_malformed_manifest():
    with tempfile.TemporaryDirectory() as tmp:
        plugin_dir = os.path.join(tmp, "malformed")
        os.makedirs(plugin_dir)
        with open(os.path.join(plugin_dir, "manifest.json"), "w") as fh:
            fh.write("{not valid json")
        discovered = plugin_loader.discover_plugins(tmp)
        assert discovered == []


def test_cleanup_extracted_noop_for_unknown_plugin_id():
    # Should not raise even if the plugin_id was never recorded (e.g. never a zip bundle).
    plugin_loader.cleanup_extracted({}, "never-existed")


def test_entrypoint_resolution_supports_bare_module_name_without_py_suffix():
    with tempfile.TemporaryDirectory() as tmp:
        plugin_dir = _write_plugin_package(tmp, "bare-entry", entrypoint="plugin")
        discovered = plugin_loader.discover_plugins(tmp)
        module = plugin_loader.import_entrypoint(discovered[0])
        assert hasattr(module, "before_workflow")


# --- Registry edge cases ----------------------------------------------------

def test_registry_disable_nonexistent_raises():
    registry = PluginRegistry()
    with pytest.raises(PluginNotFoundError):
        registry.disable("ghost")


def test_registry_mark_error_nonexistent_raises():
    registry = PluginRegistry()
    with pytest.raises(PluginNotFoundError):
        registry.mark_error("ghost", "e")


def test_registry_list_capabilities_enabled_only_false_includes_disabled():
    registry = PluginRegistry()
    registry.install(_manifest("cap-disabled", hooks=["shutdown"], permissions=["shell"]),
                      PluginSourceType.PYTHON_PACKAGE)
    assert registry.list_capabilities(enabled_only=True) == []
    caps = registry.list_capabilities(enabled_only=False)
    assert caps[0].plugin_id == "cap-disabled"


def test_registry_reenable_after_disable_clears_disabled_at():
    registry = PluginRegistry()
    registry.install(_manifest("re-enable"), PluginSourceType.PYTHON_PACKAGE, enabled_by_default=True)
    registry.disable("re-enable")
    record = registry.enable("re-enable")
    assert record.disabled_at is None
    assert record.state == PluginState.ENABLED


# --- Runtime edge cases ------------------------------------------------------

def test_context_for_plugin_preserves_config_and_settings_regardless_of_permissions():
    base = PluginContext(config={"k": "v"}, settings={"s": 1})
    scoped = context_for_plugin(base, [])
    assert scoped.config == {"k": "v"}
    assert scoped.settings == {"s": 1}


def test_dispatch_hook_multiple_plugins_all_succeed():
    def make_fn(tag):
        def fn(ctx, **kw):
            return tag
        return fn

    records = [PluginRecord(manifest=_manifest(f"multi-{i}", hooks=["shutdown"]), state=PluginState.ENABLED)
               for i in range(4)]
    modules = {r.manifest.id: _FakeModule(make_fn(r.manifest.id), "shutdown") for r in records}
    results = dispatch_hook(HookType.SHUTDOWN, records, modules, PluginContext())
    assert len(results) == 4
    assert all(r.success for r in results)


def test_dispatch_hook_passes_through_kwargs():
    captured = {}

    def fn(ctx, **kw):
        captured.update(kw)
        return None

    record = PluginRecord(manifest=_manifest("kwargs-plugin", hooks=["artifact_created"]), state=PluginState.ENABLED)
    modules = {"kwargs-plugin": _FakeModule(fn, "artifact_created")}
    dispatch_hook(HookType.ARTIFACT_CREATED, [record], modules, PluginContext(), artifact_id="abc123")
    assert captured == {"artifact_id": "abc123"}


def test_compute_health_partial_failures_computes_success_rate():
    from services.integration.plugin_sdk.plugin_models import HookExecutionResult
    executions = [
        HookExecutionResult(plugin_id="p1", hook=HookType.STARTUP, success=True, executed_at="2026-01-01T00:00:00+00:00"),
        HookExecutionResult(plugin_id="p1", hook=HookType.STARTUP, success=True, executed_at="2026-01-02T00:00:00+00:00"),
        HookExecutionResult(plugin_id="p1", hook=HookType.STARTUP, success=False, error="e",
                             executed_at="2026-01-03T00:00:00+00:00"),
    ]
    health = compute_health("p1", PluginState.ENABLED, executions)
    assert health.total_executions == 3
    assert health.failed_executions == 1
    assert round(health.success_rate, 2) == 0.67
    assert health.healthy is True  # >= 0.5 threshold


# --- Export edge cases --------------------------------------------------------

def test_export_inventory_empty_list():
    assert json.loads(plugin_export.export_inventory([], "json")) == []
    assert "0" in plugin_export.export_inventory([], "markdown")


def test_export_validation_all_valid_plugins_no_issue_rows():
    results = plugin_validator.validate_all([_manifest("clean-1"), _manifest("clean-2")])
    md = plugin_export.export_validation(results, "markdown")
    assert "0 error(s)" in md


# --- API edge cases ------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_capabilities_empty_when_no_plugins(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.get("/plugins/capabilities")
        assert resp.json()["capabilities"] == []


@pytest.mark.asyncio
async def test_api_hooks_empty_when_no_plugins_enabled(app_no_db):
    async with _client(app_no_db) as client:
        await client.post("/plugins/install", json={"plugin_id": "api-plugin"})  # installed, not enabled
        resp = await client.get("/plugins/hooks")
        assert resp.json() == {}


@pytest.mark.asyncio
async def test_api_health_empty_when_no_plugins(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.get("/plugins/health")
        assert resp.json()["count"] == 0


@pytest.mark.asyncio
async def test_api_disable_nonexistent_404(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.post("/plugins/disable", json={"plugin_id": "ghost"})
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_api_reload_plugin_not_installed_404(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.post("/plugins/reload", json={"plugin_id": "never-installed"})
        assert resp.status_code == 404


# --- CLI edge cases --------------------------------------------------------------

def test_cli_validate_json_format(capsys):
    from services.integration.plugin_sdk import plugin_cli
    with tempfile.TemporaryDirectory() as tmp:
        _write_plugin_package(tmp, "json-format-plugin")
        rc = plugin_cli.main(["--plugins-dir", tmp, "plugin", "validate", "--format", "json"])
        assert rc == 0
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert "json-format-plugin" in parsed


def test_cli_list_shows_multiple_plugins(capsys):
    from services.integration.plugin_sdk import plugin_cli
    with tempfile.TemporaryDirectory() as tmp:
        _write_plugin_package(tmp, "cli-multi-1")
        _write_plugin_package(tmp, "cli-multi-2")
        rc = plugin_cli.main(["--plugins-dir", tmp, "plugin", "list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "cli-multi-1" in out and "cli-multi-2" in out


# ══════════════════════════════════════════════════════════════
# Audit fix regression — zip-slip protection in import_entrypoint's
# zip_bundle path (services/integration/plugin_sdk/plugin_loader.py's
# _safe_extractall). See docs/PROJECT_AUDIT.md / SECURITY_REPORT.md.
# ══════════════════════════════════════════════════════════════

def test_safe_extractall_extracts_legitimate_zip_bundle():
    import zipfile
    from services.integration.plugin_sdk.plugin_loader import _safe_extractall
    with tempfile.TemporaryDirectory() as src_dir, tempfile.TemporaryDirectory() as out_dir:
        zip_path = os.path.join(src_dir, "good.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("manifest.json", "{}")
            zf.writestr("plugin.py", "x = 1")
        with zipfile.ZipFile(zip_path) as zf:
            _safe_extractall(zf, out_dir)
        assert set(os.listdir(out_dir)) == {"manifest.json", "plugin.py"}


def test_safe_extractall_rejects_path_traversal_member():
    import zipfile
    from services.integration.plugin_sdk.plugin_loader import UnsafeZipMemberError, _safe_extractall
    with tempfile.TemporaryDirectory() as src_dir, tempfile.TemporaryDirectory() as out_dir:
        zip_path = os.path.join(src_dir, "evil.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("../../evil.py", "pwned = 1")
        with pytest.raises(UnsafeZipMemberError):
            with zipfile.ZipFile(zip_path) as zf:
                _safe_extractall(zf, out_dir)
        # nothing from the malicious bundle should have been written anywhere reachable
        assert os.listdir(out_dir) == []


def test_safe_extractall_rejects_absolute_path_member():
    import zipfile
    from services.integration.plugin_sdk.plugin_loader import UnsafeZipMemberError, _safe_extractall
    with tempfile.TemporaryDirectory() as src_dir, tempfile.TemporaryDirectory() as out_dir:
        zip_path = os.path.join(src_dir, "evil_abs.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("/etc/evil.py", "pwned = 1")
        with pytest.raises(UnsafeZipMemberError):
            with zipfile.ZipFile(zip_path) as zf:
                _safe_extractall(zf, out_dir)


def test_import_entrypoint_zip_bundle_still_works_end_to_end():
    """End-to-end: import_entrypoint's real zip_bundle path (not just
    the _safe_extractall helper in isolation) still imports a
    legitimate zipped plugin correctly after the audit fix."""
    import zipfile
    from services.integration.plugin_sdk.plugin_loader import discover_plugins, import_entrypoint
    with tempfile.TemporaryDirectory() as plugins_dir:
        zip_path = os.path.join(plugins_dir, "zipped_plugin.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("manifest.json", json.dumps({
                "id": "zipped_plugin", "name": "Zipped Plugin", "version": "1.0.0", "author": "Test Author",
                "entrypoint": "plugin.py", "permissions": [], "hooks": ["startup"],
            }))
            zf.writestr("plugin.py", "def startup(ctx, **kwargs):\n    return {'ok': True}\n")
        discovered = discover_plugins(plugins_dir)
        target = next(p for p in discovered if p.manifest.id == "zipped_plugin")
        module = import_entrypoint(target)
        assert hasattr(module, "startup")
        assert module.startup(None)["ok"] is True
