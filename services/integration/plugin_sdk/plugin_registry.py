"""
services/integration/plugin_sdk/plugin_registry.py
=================================
M4.7 §3 Registry — process-local bookkeeping of installed/enabled/
disabled plugins, their metadata/versions, and capability lookup
(hooks/permissions). Same "process-local now, durable history is the
repository's job" convention `benchmark_registry.py`/`ActiveRunTracker`
established for M4.4/M4.5: `PluginRegistry` is the live, in-memory
source of truth an API/CLI process consults on every request;
`plugin_repository.py` is what makes installs/state changes durable
across process restarts.

Thread-safe (a lock around every mutation) since plugin install/enable/
disable can plausibly race with a hook dispatch reading the enabled
set from a different request in the same process — same rationale
`chaos_dashboard.ActiveRunTracker` gives for its own lock.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional

from services.integration.plugin_sdk.plugin_models import (
    CapabilityInfo, PluginManifest, PluginRecord, PluginSourceType, PluginState,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PluginAlreadyInstalledError(Exception):
    pass


class PluginNotFoundError(Exception):
    pass


class PluginRegistry:

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._plugins: Dict[str, PluginRecord] = {}

    def install(
        self, manifest: PluginManifest, source_type: PluginSourceType, source_path: Optional[str] = None,
        enabled_by_default: bool = False,
    ) -> PluginRecord:
        """Registers a new plugin in `INSTALLED` state (or `ENABLED`
        if `enabled_by_default`). Raises `PluginAlreadyInstalledError`
        if `manifest.id` is already registered — a caller wanting to
        upgrade an existing plugin should `remove` first (brief's own
        "duplicate IDs" validation is a separate, static check over a
        *candidate* manifest set before anything is installed; this is
        the runtime analog once installation is actually attempted)."""
        with self._lock:
            if manifest.id in self._plugins:
                raise PluginAlreadyInstalledError(f"plugin {manifest.id!r} is already installed")
            record = PluginRecord(
                manifest=manifest, state=PluginState.ENABLED if enabled_by_default else PluginState.INSTALLED,
                source_type=source_type, source_path=source_path, installed_at=_now_iso(),
                enabled_at=_now_iso() if enabled_by_default else None,
            )
            self._plugins[manifest.id] = record
            return record

    def remove(self, plugin_id: str) -> None:
        with self._lock:
            if plugin_id not in self._plugins:
                raise PluginNotFoundError(f"plugin {plugin_id!r} is not installed")
            del self._plugins[plugin_id]

    def enable(self, plugin_id: str) -> PluginRecord:
        with self._lock:
            record = self._require(plugin_id)
            record.state = PluginState.ENABLED
            record.enabled_at = _now_iso()
            record.disabled_at = None
            return record

    def disable(self, plugin_id: str) -> PluginRecord:
        with self._lock:
            record = self._require(plugin_id)
            record.state = PluginState.DISABLED
            record.disabled_at = _now_iso()
            return record

    def mark_error(self, plugin_id: str, error: str) -> PluginRecord:
        """A plugin that fails badly enough (e.g. its entrypoint
        raises on import during a reload) moves to `ERROR` state
        rather than staying silently `ENABLED` — brief's §6 "plugin
        health" surfaces this; the plugin is not automatically
        removed (a human should decide that), only marked."""
        with self._lock:
            record = self._require(plugin_id)
            record.state = PluginState.ERROR
            record.last_error = error
            return record

    def get(self, plugin_id: str) -> Optional[PluginRecord]:
        with self._lock:
            return self._plugins.get(plugin_id)

    def _require(self, plugin_id: str) -> PluginRecord:
        record = self._plugins.get(plugin_id)
        if record is None:
            raise PluginNotFoundError(f"plugin {plugin_id!r} is not installed")
        return record

    def list_all(self) -> List[PluginRecord]:
        with self._lock:
            return sorted(self._plugins.values(), key=lambda r: r.manifest.id)

    def list_enabled(self) -> List[PluginRecord]:
        return [r for r in self.list_all() if r.state == PluginState.ENABLED]

    def list_disabled(self) -> List[PluginRecord]:
        return [r for r in self.list_all() if r.state == PluginState.DISABLED]

    def list_installed(self) -> List[PluginRecord]:
        """Every registered plugin regardless of enabled/disabled/
        error state — "installed" in the broad sense of "known to the
        registry," matching brief's §3 "Installed plugins" as
        distinct from the enabled/disabled subsets."""
        return self.list_all()

    # ── Capability discovery (brief §6) ────────────────────────────

    def list_hooks(self, enabled_only: bool = True) -> Dict[str, List[str]]:
        """hook name -> sorted list of plugin ids that declare it —
        brief's "list hooks.\""""
        records = self.list_enabled() if enabled_only else self.list_all()
        out: Dict[str, List[str]] = {}
        for record in records:
            for hook in record.manifest.hooks:
                out.setdefault(hook, []).append(record.manifest.id)
        return {hook: sorted(ids) for hook, ids in out.items()}

    def list_permissions(self, enabled_only: bool = True) -> Dict[str, List[str]]:
        """permission name -> sorted list of plugin ids that declare
        it — brief's "list permissions.\""""
        records = self.list_enabled() if enabled_only else self.list_all()
        out: Dict[str, List[str]] = {}
        for record in records:
            for perm in record.manifest.permissions:
                out.setdefault(perm, []).append(record.manifest.id)
        return {perm: sorted(ids) for perm, ids in out.items()}

    def list_capabilities(self, enabled_only: bool = True) -> List[CapabilityInfo]:
        """Brief's "list capabilities" — one `CapabilityInfo` per
        plugin (hooks + permissions together), rather than the
        hook-keyed/permission-keyed views `list_hooks`/`list_permissions`
        give, for a caller that wants "what can plugin X do" instead of
        "who can do X.\""""
        records = self.list_enabled() if enabled_only else self.list_all()
        return [CapabilityInfo(plugin_id=r.manifest.id, hooks=list(r.manifest.hooks),
                                permissions=list(r.manifest.permissions)) for r in records]

    def has_permission(self, plugin_id: str, permission: str) -> bool:
        record = self.get(plugin_id)
        return record is not None and permission in record.manifest.permissions


default_registry = PluginRegistry()
