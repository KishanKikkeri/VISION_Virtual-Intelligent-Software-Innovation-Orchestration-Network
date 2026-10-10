"""
services/integration/plugin_sdk/sandbox/sandbox_context.py
=================================
M5.1 §"Sandbox Context" — "The wrapper should enforce permissions, not
just document them." Two distinct mechanisms depending on execution
mode, because the two modes have fundamentally different capability
boundaries:

- **`NONE` mode**: reuses `plugin_runtime.context_for_plugin` directly
  (imported, not duplicated) — that function already enforces
  permissions correctly today (a plugin without `"artifacts"` gets
  `artifact_manager=None`); this milestone adds nothing here for
  `NONE` mode beyond calling the existing function.
- **`PROCESS` mode**: `build_context_payload` below produces a
  `SandboxContextPayload` (JSON-only) for the child process, and
  `SandboxContextProxy` is what the *child* actually constructs from
  that payload and hands to the plugin's hook function — its
  `__getattr__` raises a structured `SandboxPermissionError` (not a
  bare `AttributeError`) for any capability the policy didn't grant,
  so a denial is a reportable, loggable event (feeding
  `SandboxViolation`), not just a crash with no diagnostic trail.
"""
from __future__ import annotations

from typing import Any, Dict

from services.integration.plugin_sdk.plugin_models import Permission
from services.integration.plugin_sdk.plugin_runtime import PluginContext, context_for_plugin
from services.integration.plugin_sdk.sandbox.sandbox_models import SandboxContextPayload, SandboxPolicy


class SandboxPermissionError(PermissionError):
    """Raised by `SandboxContextProxy.__getattr__` when a plugin
    running in PROCESS mode accesses a capability its policy didn't
    grant. Deliberately a subclass of the builtin `PermissionError`
    (not a bespoke unrelated exception type) so a plugin author's own
    `except PermissionError:` handling, if any, still works."""
    def __init__(self, plugin_id: str, attribute: str):
        self.plugin_id = plugin_id
        self.attribute = attribute
        super().__init__(f"plugin {plugin_id!r} attempted to access {attribute!r}, which its sandbox policy "
                          f"does not grant")


def build_none_mode_context(base_context: PluginContext, manifest_permissions: list) -> PluginContext:
    """`NONE` mode's context — delegates entirely to the existing,
    already-correct `context_for_plugin`. Exists as a named function
    (rather than callers importing `context_for_plugin` directly) only
    so `sandbox_runtime.SandboxManager` has one call per mode with a
    parallel name, not because the behavior differs."""
    return context_for_plugin(base_context, manifest_permissions)


def build_context_payload(base_context: PluginContext, policy: SandboxPolicy) -> SandboxContextPayload:
    """`PROCESS` mode's context — the JSON-serializable payload sent to
    the child. Only `config`/`settings` (already plain dicts on
    `PluginContext`) can cross the process boundary at all; this
    function does not attempt to serialize `repository`/
    `artifact_manager`/`event_publisher`/`runtime` because they
    physically cannot be (see `sandbox_models.SandboxContextPayload`'s
    docstring) — this is why `sandbox_policy.PROCESS_MODE_UNAVAILABLE`
    exists, and this function trusts that the caller already applied
    it when building `policy.allowed_permissions`."""
    return SandboxContextPayload(
        config=dict(base_context.config),
        settings=dict(base_context.settings),
        allowed_permissions=list(policy.allowed_permissions),
    )


class SandboxContextProxy:
    """What a plugin's hook function actually receives as `ctx` when
    running in PROCESS mode (constructed by `sandbox_runner.py` inside
    the child process from a `SandboxContextPayload`). Exposes
    `config`/`settings` unconditionally (matching `context_for_plugin`'s
    own "always present" fields) and raises `SandboxPermissionError`
    for any other attribute access — there is no live `repository`/
    `artifact_manager`/`event_publisher`/`runtime` to conditionally
    allow in this mode at all (see module docstring), so every
    capability beyond config/settings is denied, not "conditionally
    allowed," in PROCESS mode today."""

    def __init__(self, plugin_id: str, payload: SandboxContextPayload):
        self._plugin_id = plugin_id
        self._payload = payload
        self.config: Dict[str, Any] = dict(payload.config)
        self.settings: Dict[str, Any] = dict(payload.settings)

    def has_permission(self, permission: Permission) -> bool:
        return permission in self._payload.allowed_permissions

    def __getattr__(self, name: str) -> Any:
        # __getattr__ only fires for attributes NOT already set in
        # __init__ (config/settings), so this only ever handles the
        # denied set.
        if name.startswith("_"):
            raise AttributeError(name)
        raise SandboxPermissionError(self._plugin_id, name)


def build_sandbox_context_proxy(plugin_id: str, payload: SandboxContextPayload) -> SandboxContextProxy:
    return SandboxContextProxy(plugin_id, payload)
