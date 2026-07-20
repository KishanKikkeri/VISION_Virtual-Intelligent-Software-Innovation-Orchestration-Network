"""
services/integration/plugin_sdk/plugin_runtime.py
=================================
M4.7 §5 Plugin Context + §4 Hook System — the one module that actually
calls into a loaded plugin module's functions. Two responsibilities:

  1. **`PluginContext`** (§5): the object every plugin hook function
     receives. "No global imports" (brief) means a plugin never reaches
     for a platform-wide singleton/import to get a repository, logger,
     etc. — it receives everything through this one parameter.
     `context_for_plugin` additionally *scopes* the context to what a
     plugin's manifest actually declared permission for (§7's
     permission system, enforced here rather than only validated
     statically — see its own docstring).

  2. **`dispatch_hook`** (§4): calls every enabled plugin's function
     for one `HookType`, in deterministic order (sorted by plugin id),
     catching every exception per-plugin so "failures must never crash
     platform" (brief, repeated in both §4 and the closing Important
     Constraints) is true by construction, not by convention.

A plugin's hook function is found on its imported module by name —
`getattr(module, hook.value, None)` — matching the hook value exactly
(e.g. a module defining `def before_workflow(ctx, **kwargs): ...`
implements that hook). A plugin whose manifest *declares* a hook it
does not actually implement is not an error at dispatch time (brief
never asks for a required-function check, only a valid-hook-name
check — see `plugin_validator.validate_hooks`'s docstring on where
that line is drawn); it's simply skipped for that hook, with no
`HookExecutionResult` recorded for it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from services.integration.plugin_sdk.plugin_models import (
    HookExecutionResult, HookType, PluginHealth, PluginRecord, PluginState,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PluginContext:
    """Brief §5's exact field list. Not a Pydantic model (deliberately):
    `repository`/`artifact_manager`/`event_publisher`/`runtime` are
    live objects/callables (a repository session, a publish function,
    the registry itself), not serializable data — a `dataclass` is the
    right shape here, same as `chaos_dashboard.ActiveRunTracker` (also
    a plain class, not a Pydantic model, for the same "holds live
    objects" reason)."""
    logger: Any = None
    config: Dict[str, Any] = field(default_factory=dict)
    repository: Optional[Any] = None
    artifact_manager: Optional[Any] = None
    event_publisher: Optional[Callable[..., Any]] = None
    runtime: Optional[Any] = None
    settings: Dict[str, Any] = field(default_factory=dict)


def context_for_plugin(base_context: PluginContext, permissions: List[str]) -> PluginContext:
    """§7 Permission System, enforced at the one place a plugin
    actually touches platform capabilities: builds a *copy* of
    `base_context` with capability fields blanked out unless the
    plugin's manifest declared the corresponding permission —

        artifact_manager  requires "artifacts"
        event_publisher   requires "events"
        repository        requires "read" or "write"

    `logger`/`config`/`runtime`/`settings` are always present (a
    plugin needs at least logging and its own config to function at
    all, and `runtime`/`settings` carry no platform-mutating
    capability by themselves). A plugin that didn't declare
    `\"artifacts\"` and tries to use `ctx.artifact_manager` gets `None`,
    not an exception — the same "absence, not an error" convention
    `MetricsSnapshot`/other M4.x "missing infra" shapes use, so a
    plugin can defensively check `if ctx.artifact_manager:` rather
    than needing a try/except around every capability access."""
    perms = set(permissions)
    return PluginContext(
        logger=base_context.logger,
        config=dict(base_context.config),
        repository=base_context.repository if ({"read", "write"} & perms) else None,
        artifact_manager=base_context.artifact_manager if "artifacts" in perms else None,
        event_publisher=base_context.event_publisher if "events" in perms else None,
        runtime=base_context.runtime,
        settings=dict(base_context.settings),
    )


def dispatch_hook(
    hook: HookType, records: List[PluginRecord], modules: Dict[str, Any], base_context: PluginContext,
    **kwargs: Any,
) -> List[HookExecutionResult]:
    """Brief §4 entry point. `records` should be the caller's enabled-
    plugin list (typically `PluginRegistry.list_enabled()`); `modules`
    maps plugin id -> its already-imported module (via
    `plugin_loader.import_entrypoint`) — this function does no
    importing itself, so it's testable against plain synthetic module
    stand-ins (see `tests/foundation/test_m47_plugin_sdk.py`).

    **Deterministic order**: `records` is iterated in the order
    given; callers should pass an already-id-sorted list (which is
    what `PluginRegistry.list_enabled()` returns) rather than this
    function re-sorting, so a caller with its own explicit priority
    order (a future increment) is not silently overridden.

    **Failure isolation**: every plugin's hook function call is
    individually wrapped in try/except — one plugin raising produces
    a `success=False` `HookExecutionResult` for that plugin only;
    every other plugin's hook still runs."""
    results: List[HookExecutionResult] = []
    for record in records:
        module = modules.get(record.manifest.id)
        if module is None:
            continue
        hook_fn = getattr(module, hook.value, None)
        if hook_fn is None:
            continue  # plugin doesn't implement this hook — not an error, see module docstring

        scoped_context = context_for_plugin(base_context, record.manifest.permissions)
        start = time.perf_counter()
        try:
            result = hook_fn(scoped_context, **kwargs)
            results.append(HookExecutionResult(
                plugin_id=record.manifest.id, hook=hook, success=True,
                duration_ms=(time.perf_counter() - start) * 1000.0, executed_at=_now_iso(), result=result,
            ))
        except Exception as e:  # noqa: BLE001 — brief: "Failures must never crash platform"
            results.append(HookExecutionResult(
                plugin_id=record.manifest.id, hook=hook, success=False,
                duration_ms=(time.perf_counter() - start) * 1000.0, executed_at=_now_iso(), error=str(e),
            ))
    return results


def compute_health(plugin_id: str, state: PluginState, executions: List[HookExecutionResult]) -> PluginHealth:
    """Brief §6 "plugin health" — derived purely from a plugin's own
    execution history (already-collected `HookExecutionResult`s,
    whether from an in-memory run or replayed from
    `plugin_repository.py`'s persisted rows), same "measured
    elsewhere, derived here" split `resilience_analyzer.py`/
    `posture_analyzer.py` established. A plugin with zero executions
    yet is reported `healthy=True` with `success_rate=1.0` — no
    evidence of failure is not itself evidence of failure, same
    "missing is not zero, missing is not measured" convention used
    throughout the platform's M4.x modules."""
    total = len(executions)
    failed = sum(1 for e in executions if not e.success)
    success_rate = ((total - failed) / total) if total else 1.0
    last = max(executions, key=lambda e: e.executed_at) if executions else None
    return PluginHealth(
        plugin_id=plugin_id, state=state, healthy=(state != PluginState.ERROR and success_rate >= 0.5),
        total_executions=total, failed_executions=failed, success_rate=round(success_rate, 4),
        last_execution_at=last.executed_at if last else None,
        last_error=(last.error if last and not last.success else None),
    )
