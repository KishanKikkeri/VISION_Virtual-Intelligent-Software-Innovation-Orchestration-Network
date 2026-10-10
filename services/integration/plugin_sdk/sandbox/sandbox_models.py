"""
services/integration/plugin_sdk/sandbox/sandbox_models.py
=================================
M5.1 Plugin Sandboxing — pure Pydantic shapes, no subprocess/FastAPI/
SQLAlchemy import anywhere in this module, matching every other
`*_models.py` in this repository's layering convention (see
`release_validation/release_validation_models.py`'s own docstring for
the same rule stated explicitly).

**Additive, not a redesign.** `services.integration.plugin_sdk.
plugin_models.PluginManifest`/`PluginRecord`/`HookType` are untouched
and reused here (imported, not duplicated) — this module only adds
the *new* shapes M5.1 needs: execution policy, permissions, IPC
message shapes, and the monitoring/persistence records. Nothing here
redefines a concept `plugin_models.py` already owns.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExecutionPolicy(str, Enum):
    """Only two modes are implemented in M5.1, per the milestone brief.
    `NONE` preserves today's exact behavior (a plugin's hook function
    is called in-process, via the existing `plugin_runtime.dispatch_
    hook`/`context_for_plugin` — this module never re-implements that
    path, `sandbox_runtime.py` calls it directly for `NONE`). `PROCESS`
    is the new isolation mode this milestone adds (see `sandbox_
    runner.py`/`sandbox_runtime.py`).

    `CONTAINER`/`FIRECRACKER`/`WASM` are deliberately **not** members
    of this enum — the brief asks for "design only, no implementation"
    for those, and an enum member with no runner behind it is worse
    than no member at all (a caller could select it and get a
    confusing `NotImplementedError` deep in dispatch instead of a
    clear validation error at policy-construction time). The extension
    point for them is `SandboxRunnerProtocol` in `sandbox_runtime.py` —
    adding a new mode later means adding one new enum member here and
    one new class implementing that protocol, no change to
    `SandboxManager`'s dispatch logic."""
    NONE = "none"
    PROCESS = "process"


from services.integration.plugin_sdk.plugin_models import Permission


class ViolationKind(str, Enum):
    DENIED_PERMISSION = "denied_permission"
    LIMIT_EXCEEDED = "limit_exceeded"
    TIMEOUT = "timeout"
    CRASH = "crash"
    MALFORMED_PAYLOAD = "malformed_payload"


# ── Runtime limits ────────────────────────────────────────────────────

class SandboxLimits(BaseModel):
    """§"Runtime Limits". `max_memory_mb` is deliberately present as a
    field (so the shape is future-proof/extensible per the brief) even
    though this milestone's PROCESS-mode enforcement of it is
    best-effort and platform-dependent — see `sandbox_limits.py`'s own
    docstring for exactly what is and isn't enforced today, stated
    honestly rather than implied as guaranteed by this field existing."""
    wall_timeout_seconds: float = 10.0
    cpu_timeout_seconds: Optional[float] = 5.0
    max_stdout_bytes: int = 65536
    max_stderr_bytes: int = 65536
    max_memory_mb: Optional[int] = 256


# ── Policy ─────────────────────────────────────────────────────────────

class SandboxPolicy(BaseModel):
    """One policy per plugin id. `allowed_permissions` is the resolved
    (post-alias-mapping) `Permission` set this plugin's manifest
    actually grants — computed once by `sandbox_policy.build_policy`,
    not re-derived at every dispatch."""
    plugin_id: str
    execution_mode: ExecutionPolicy = ExecutionPolicy.NONE
    allowed_permissions: List[Permission] = Field(default_factory=list)
    limits: SandboxLimits = Field(default_factory=SandboxLimits)
    updated_at: str = Field(default_factory=_now_iso)

    def allows(self, permission: Permission) -> bool:
        return permission in self.allowed_permissions


# ── IPC message shapes (§"IPC") ────────────────────────────────────────

class SandboxContextPayload(BaseModel):
    """The JSON-serializable subset of `plugin_runtime.PluginContext`
    that can actually cross a process boundary. **This is a hard
    constraint, not a policy choice**: `PluginContext.repository`/
    `.artifact_manager`/`.event_publisher`/`.runtime` are live Python
    objects (a DB session, a bound method, the registry itself) that
    cannot be JSON-serialized at all — see `sandbox_context.py`'s
    docstring for the full reasoning. Only `config`/`settings` (already
    plain dicts on `PluginContext`) are carried across."""
    config: Dict[str, Any] = Field(default_factory=dict)
    settings: Dict[str, Any] = Field(default_factory=dict)
    allowed_permissions: List[Permission] = Field(default_factory=list)


class SandboxRequest(BaseModel):
    """What the parent process writes to the request file for
    `sandbox_runner.py` (the child process entrypoint) to read.
    `plugins_dir`/`plugin_id` (not a pre-imported module object — the
    child starts a fresh interpreter and cannot receive one) let the
    child re-run `plugin_loader.discover_plugins`/`import_entrypoint`
    itself."""
    plugin_id: str
    hook: str
    plugins_dir: str
    kwargs: Dict[str, Any] = Field(default_factory=dict)
    context: SandboxContextPayload = Field(default_factory=SandboxContextPayload)
    limits: SandboxLimits = Field(default_factory=SandboxLimits)


class SandboxLog(BaseModel):
    plugin_id: str
    level: str = "info"
    message: str
    timestamp: str = Field(default_factory=_now_iso)


class SandboxViolation(BaseModel):
    plugin_id: str
    kind: ViolationKind
    detail: str
    permission: Optional[Permission] = None
    timestamp: str = Field(default_factory=_now_iso)


class SandboxResult(BaseModel):
    """What `sandbox_runner.py` writes to the result file, and what
    `sandbox_runtime.py`'s runners return uniformly regardless of
    execution mode (`NONE` synthesizes one too, for a single return
    shape `SandboxManager` callers can rely on)."""
    plugin_id: str
    hook: str
    success: bool
    result: Optional[Any] = None
    error: Optional[str] = None
    timed_out: bool = False
    crashed: bool = False
    duration_ms: float = 0.0
    stdout: str = ""
    stderr: str = ""
    logs: List[SandboxLog] = Field(default_factory=list)
    violations: List[SandboxViolation] = Field(default_factory=list)
    generated_at: str = Field(default_factory=_now_iso)


# ── Monitoring (§"Monitoring") ─────────────────────────────────────────

class SandboxMetrics(BaseModel):
    plugin_id: str
    execution_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    timeout_count: int = 0
    crash_count: int = 0
    violation_count: int = 0
    total_duration_ms: float = 0.0

    @property
    def average_duration_ms(self) -> float:
        return round(self.total_duration_ms / self.execution_count, 2) if self.execution_count else 0.0


class SandboxDashboardSummary(BaseModel):
    """§"Dashboard" — the one new card this milestone adds. Optional/
    additive on `PlatformDashboard`, same convention `ReleaseSummary`
    (M4.10) established."""
    healthy: bool = True
    execution_mode: ExecutionPolicy = ExecutionPolicy.NONE
    total_executions: int = 0
    violation_count: int = 0
    timeout_count: int = 0
    failure_count: int = 0
    average_runtime_ms: float = 0.0


# ── Persistence records (§"Repository") ────────────────────────────────

class SandboxExecutionRecord(BaseModel):
    id: Optional[int] = None
    plugin_id: str
    hook: str
    execution_mode: ExecutionPolicy
    success: bool
    timed_out: bool
    crashed: bool
    duration_ms: float
    error: Optional[str] = None
    created_at: str = Field(default_factory=_now_iso)


class SandboxPolicyRecord(BaseModel):
    id: Optional[int] = None
    plugin_id: str
    policy: SandboxPolicy
    created_at: str = Field(default_factory=_now_iso)


class SandboxViolationRecord(BaseModel):
    id: Optional[int] = None
    plugin_id: str
    kind: ViolationKind
    detail: str
    permission: Optional[Permission] = None
    created_at: str = Field(default_factory=_now_iso)
