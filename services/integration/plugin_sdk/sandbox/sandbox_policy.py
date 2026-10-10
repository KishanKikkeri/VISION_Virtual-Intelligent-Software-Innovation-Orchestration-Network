"""
services/integration/plugin_sdk/sandbox/sandbox_policy.py
=================================
M5.1 §"Permissions" — builds the default-deny `SandboxPolicy` every
plugin gets from its manifest. Reuses `plugin_models.Permission`
directly (READ/WRITE/EVENTS/ARTIFACTS/NETWORK/FILESYSTEM/SHELL) rather
than inventing a second permission vocabulary — that enum is already
the one `plugin_validator.validate_permissions` enforces against a
manifest's `permissions: List[str]` (`PLUGIN-UNKNOWN-PERMISSION`,
severity `"error"` — confirmed by reading `plugin_validator.py`
directly), so it is already the authoritative "what can a manifest
even say" vocabulary. Defining a second one here would create exactly
the kind of duplicated-vocabulary problem this project's own audits
(`ARCHITECTURE_AUDIT.md`) look for — this module deliberately avoids
that.

**Default-deny is the core security property**: a plugin only gains a
`Permission` by its manifest naming the matching string (or a
policy-override widening it explicitly) — a manifest with `permissions:
[]` (or one containing only unrecognized strings) gets a `SandboxPolicy`
with an empty `allowed_permissions` list, denying everything.
"""
from __future__ import annotations

from typing import Iterable, List, Optional

from services.integration.plugin_sdk.plugin_models import Permission
from services.integration.plugin_sdk.sandbox.sandbox_models import ExecutionPolicy, SandboxLimits, SandboxPolicy

_KNOWN_PERMISSION_VALUES = {p.value for p in Permission}

# Permissions PROCESS mode can never grant regardless of manifest
# content, because the capability behind them (a live DB session via
# `PluginContext.repository`, a live event-publisher callable) cannot
# cross a process boundary at all in this milestone's IPC design
# (JSON-only, no pickle — see `sandbox_models.SandboxContextPayload`'s
# docstring). Listed here so `resolve_permissions_for_mode` has one
# place to consult, not a scattered set of special cases.
PROCESS_MODE_UNAVAILABLE: frozenset = frozenset({Permission.READ, Permission.WRITE, Permission.ARTIFACTS,
                                                  Permission.EVENTS})

# §"Sandbox Context" — "Denied by default: ctx.network, ctx.filesystem,
# ctx.shell, ctx.subprocess." Unlike the DB/artifacts/events
# permissions above (unavailable in PROCESS mode purely for
# IPC-serialization reasons, and *are* available in NONE mode exactly
# as they are today), NETWORK/FILESYSTEM/SHELL are denied in **every**
# mode, including NONE — no existing `plugin_runtime.context_for_plugin`
# code path grants any of these three today, and this milestone does
# not add a mechanism to grant them, matching the brief's explicit
# "denied by default" list. A manifest that lists `"network"`,
# `"filesystem"`, or `"shell"` in its `permissions` therefore has that
# entry accepted by `plugin_validator` (it's a known permission
# string) but never actually resolves to a granted capability via this
# policy module.
DENIED_IN_ALL_MODES: frozenset = frozenset({Permission.NETWORK, Permission.FILESYSTEM, Permission.SHELL})


def resolve_permissions(manifest_permissions: Iterable[str]) -> List[Permission]:
    """Pure mapping, no policy/mode awareness — every string in
    `manifest_permissions` that matches a known `Permission` value,
    in manifest order, deduplicated. An unrecognized string
    contributes nothing (default-deny, not an error)."""
    resolved: List[Permission] = []
    for raw in manifest_permissions:
        normalized = raw.strip().lower()
        if normalized in _KNOWN_PERMISSION_VALUES and Permission(normalized) not in resolved:
            resolved.append(Permission(normalized))
    return resolved


def resolve_permissions_for_mode(manifest_permissions: Iterable[str], mode: ExecutionPolicy) -> List[Permission]:
    """`resolve_permissions` narrowed by (a) `DENIED_IN_ALL_MODES`,
    always, and (b) `PROCESS_MODE_UNAVAILABLE` when `mode ==
    ExecutionPolicy.PROCESS`. A manifest declaring `"read"` still
    grants nothing DB-shaped when running in `PROCESS` mode — a known,
    documented M5.1 limitation (see the handover doc), not silently
    ignored."""
    resolved = [p for p in resolve_permissions(manifest_permissions) if p not in DENIED_IN_ALL_MODES]
    if mode == ExecutionPolicy.PROCESS:
        resolved = [p for p in resolved if p not in PROCESS_MODE_UNAVAILABLE]
    return resolved


def build_policy(plugin_id: str, manifest_permissions: Iterable[str],
                  execution_mode: ExecutionPolicy = ExecutionPolicy.NONE,
                  limits: Optional[SandboxLimits] = None) -> SandboxPolicy:
    return SandboxPolicy(
        plugin_id=plugin_id,
        execution_mode=execution_mode,
        allowed_permissions=resolve_permissions_for_mode(manifest_permissions, execution_mode),
        limits=limits or SandboxLimits(),
    )


def is_allowed(policy: SandboxPolicy, permission: Permission) -> bool:
    return policy.allows(permission)
