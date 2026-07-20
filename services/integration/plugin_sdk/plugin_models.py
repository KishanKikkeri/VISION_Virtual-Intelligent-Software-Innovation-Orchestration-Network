"""
services/integration/plugin_sdk/plugin_models.py
=================================
M4.7 — pure Pydantic shapes, no FastAPI/SQLAlchemy/importlib import,
same layering convention M4.5's `chaos_models.py` and M4.6's
`security_models.py` both established: plain models so
`plugin_validator.py`/`plugin_registry.py`/`plugin_runtime.py` stay
independently unit-testable, with no dynamic-import machinery anywhere
near the data shapes themselves.

**Why `permissions`/`hooks` are `List[str]`, not `List[Permission]`/
`List[HookType]` directly on `PluginManifest`:** a manifest with an
unknown permission or hook name must still *parse* (so
`plugin_validator.py` can report it as a named, actionable
`ValidationIssue` — brief's "Validator rejects undeclared
permissions") rather than fail construction with an opaque Pydantic
enum error. `Permission`/`HookType` are the controlled vocabularies
`plugin_validator.py` checks manifest strings against; they are not
field types on `PluginManifest` itself.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class Permission(str, Enum):
    """Brief's §7 permission catalog, verbatim."""
    READ = "read"
    WRITE = "write"
    EVENTS = "events"
    ARTIFACTS = "artifacts"
    NETWORK = "network"
    FILESYSTEM = "filesystem"
    SHELL = "shell"


class HookType(str, Enum):
    """Brief's §4 hook catalog, verbatim."""
    BEFORE_AGENT_RUN = "before_agent_run"
    AFTER_AGENT_RUN = "after_agent_run"
    BEFORE_WORKFLOW = "before_workflow"
    AFTER_WORKFLOW = "after_workflow"
    ARTIFACT_CREATED = "artifact_created"
    DEPLOYMENT_COMPLETED = "deployment_completed"
    INCIDENT_CREATED = "incident_created"
    SECURITY_SCAN_FINISHED = "security_scan_finished"
    DASHBOARD_SECTION = "dashboard_section"
    STARTUP = "startup"
    SHUTDOWN = "shutdown"


class PluginState(str, Enum):
    INSTALLED = "installed"
    ENABLED = "enabled"
    DISABLED = "disabled"
    ERROR = "error"


class PluginSourceType(str, Enum):
    """Brief's §2 "Support: python packages / zip bundles / editable
    development plugins.\""""
    PYTHON_PACKAGE = "python_package"
    ZIP_BUNDLE = "zip_bundle"
    EDITABLE = "editable"


class PluginDependency(BaseModel):
    """One entry in a manifest's `dependencies` list — another
    plugin's id plus an optional version constraint string (e.g.
    `\">=1.0.0\"`), checked by `plugin_validator.build_dependency_graph`/
    `check_version_constraint`."""
    plugin_id: str
    version_constraint: Optional[str] = None


class PluginManifest(BaseModel):
    """Brief's §1 manifest fields, verbatim. `permissions`/`hooks` are
    plain strings — see module docstring for why. `dependencies` is a
    list of `PluginDependency` (not bare strings) so a version
    constraint can travel with the dependency id."""
    id: str
    name: str
    version: str
    author: str
    description: str = ""
    entrypoint: str
    api_version: str = "1.0.0"
    permissions: List[str] = Field(default_factory=list)
    dependencies: List[PluginDependency] = Field(default_factory=list)
    hooks: List[str] = Field(default_factory=list)


class ValidationIssue(BaseModel):
    """One problem `plugin_validator.py` found — `severity` is
    `\"error\"` (blocks installation) or `\"warning\"` (installable, but
    worth surfacing) so a caller can distinguish "will not install"
    from "installs, with a caveat.\""""
    rule_id: str
    severity: str = "error"  # "error" | "warning"
    message: str
    plugin_id: Optional[str] = None


class ValidationResult(BaseModel):
    """Brief's §8 Plugin Validation output — one per plugin (or one
    aggregate result across a whole plugin set, for the
    duplicate-ids/cyclic-dependency checks that are inherently
    cross-plugin)."""
    plugin_id: Optional[str] = None
    valid: bool = True
    issues: List[ValidationIssue] = Field(default_factory=list)

    @property
    def errors(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]


class DependencyGraphResult(BaseModel):
    """`plugin_validator.build_dependency_graph`'s output. `order` is a
    topological load order (only meaningful when `cycles` is empty —
    see that function's docstring); `cycles` is a list of plugin-id
    cycles found, each cycle listed once, in the deterministic order
    they were discovered."""
    order: List[str] = Field(default_factory=list)
    cycles: List[List[str]] = Field(default_factory=list)
    missing_dependencies: Dict[str, List[str]] = Field(default_factory=dict)  # plugin_id -> [missing dep ids]


class PluginRecord(BaseModel):
    """A plugin as tracked by `plugin_registry.PluginRegistry` — the
    manifest plus install/runtime metadata. Framework-free (no
    FastAPI/SQLAlchemy), same convention every other M4.x `*Record`
    shape follows."""
    manifest: PluginManifest
    state: PluginState = PluginState.INSTALLED
    source_type: PluginSourceType = PluginSourceType.PYTHON_PACKAGE
    source_path: Optional[str] = None
    installed_at: str = ""
    enabled_at: Optional[str] = None
    disabled_at: Optional[str] = None
    last_error: Optional[str] = None


class HookExecutionResult(BaseModel):
    """One plugin's outcome for one hook dispatch — brief's "Hooks
    execute in deterministic order. Failures must never crash
    platform": a failing plugin produces a `success=False` result with
    `error` set, not an exception that propagates to the caller (see
    `plugin_runtime.dispatch_hook`)."""
    plugin_id: str
    hook: HookType
    success: bool
    duration_ms: float = 0.0
    error: Optional[str] = None
    executed_at: str = ""
    result: Optional[Any] = None


class PluginExecutionResult(BaseModel):
    """A more general plugin invocation record than
    `HookExecutionResult` — brief's `PluginExecution` table covers any
    plugin call (a hook dispatch, a capability invocation, a
    reload/health-check probe), while `PluginHookExecution` is
    specifically the hook-dispatch subset (see
    `infrastructure.database.plugin_sdk_models` module docstring for
    how the two tables relate)."""
    plugin_id: str
    action: str  # e.g. "hook:before_workflow", "capability:my_capability", "healthcheck"
    success: bool
    duration_ms: float = 0.0
    error: Optional[str] = None
    executed_at: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)


class PluginHealth(BaseModel):
    """Brief's §6 "plugin health." Derived from a plugin's execution
    history (`PluginExecutionResult`/`HookExecutionResult` records) —
    same "measured elsewhere, derived here" convention
    `resilience_analyzer.py`/`posture_analyzer.py` established for
    M4.5/M4.6."""
    plugin_id: str
    state: PluginState = PluginState.INSTALLED
    healthy: bool = True
    total_executions: int = 0
    failed_executions: int = 0
    success_rate: float = 1.0
    last_execution_at: Optional[str] = None
    last_error: Optional[str] = None


class CapabilityInfo(BaseModel):
    """One plugin's declared capabilities — brief's §6 "list
    capabilities / list hooks / list permissions.\""""
    plugin_id: str
    hooks: List[str] = Field(default_factory=list)
    permissions: List[str] = Field(default_factory=list)


class PluginInventory(BaseModel):
    """The full installed-plugin inventory — `plugin_export.py`'s
    "Plugin inventory" export source."""
    plugins: List[PluginRecord] = Field(default_factory=list)
    generated_at: str = ""


class PluginReport(BaseModel):
    """The aggregate report `plugin_export.py`/a future API route
    renders — inventory + health + validation, one shape, three
    renderers (same "compute once, render N ways" split
    `chaos_report.py`/`vulnerability_report.py` established)."""
    inventory: PluginInventory
    health: List[PluginHealth] = Field(default_factory=list)
    validation: Dict[str, ValidationResult] = Field(default_factory=dict)
    dependency_graph: Optional[DependencyGraphResult] = None
    generated_at: str = ""
