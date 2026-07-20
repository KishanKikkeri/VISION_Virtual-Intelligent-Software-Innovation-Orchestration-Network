"""
services/integration/plugin_sdk/
=================================
M4.7 — Plugin SDK & Extension Framework. See
docs/M4.7_Plugin_SDK_Handover.md for the full writeup.

Lets third parties extend AASC without modifying platform source code:
plugins are discovered and dynamically imported from a `plugins/`
directory (python packages, zip bundles, or explicit editable
development paths), validated (manifest schema, permissions, hooks,
dependency graph, version compatibility), registered, and dispatched
via a deterministic, failure-isolated hook system.

    plugin_models.py       Pure Pydantic shapes (PluginManifest, Permission,
                           HookType, PluginRecord, ValidationResult,
                           DependencyGraphResult, HookExecutionResult,
                           PluginHealth, CapabilityInfo, ...).
    plugin_manifest.py      Manifest text (JSON/TOML) -> PluginManifest.
    plugin_loader.py        Discovery (python_package / zip_bundle / editable)
                           + dynamic entrypoint import. The only module that
                           touches the filesystem/import machinery.
    plugin_validator.py     Permission/hook/version-compatibility checks,
                           duplicate-id detection, dependency graph +
                           cycle detection (build_dependency_graph).
    plugin_registry.py      Process-local installed/enabled/disabled tracking
                           + capability lookup (list_hooks/list_permissions/
                           list_capabilities).
    plugin_runtime.py       PluginContext, permission-scoped context building,
                           deterministic failure-isolated hook dispatch
                           (dispatch_hook), health derivation (compute_health).
    plugin_repository.py    Repository-pattern DB persistence (plugins /
                           plugin_installations / plugin_executions /
                           plugin_hook_executions) + fetch_plugin_dashboard_section.
    plugin_export.py        csv-free json/markdown/html export for inventory,
                           health, and validation reports.
    plugin_cli.py            python plugin_cli.py plugin [install|remove|list|
                           validate|enable|disable|reload]
"""
