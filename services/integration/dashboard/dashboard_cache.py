"""
services/integration/dashboard/dashboard_cache.py
=================================
M4.3 — a small in-memory TTL cache. The spec asks for "real-time
updates", which in practice means a UI polling every few seconds; that
should not mean re-running `workflow_validator.validate_all_workflows_
detailed()`, a full readiness report, and a cross-project audit-trail
scan on every single poll from every connected browser tab. This is
the "do not build another metrics pipeline" constraint applied to the
dashboard's own aggregation step, not just to Monitoring's.

Deliberately process-local (a plain dict, no Redis/NATS dependency) —
this platform already has a real message bus (NATS JetStream) for
cross-process concerns; a dashboard render cache with a several-second
TTL doesn't need one. If the dashboard is ever run across multiple
worker processes and per-process cache staleness becomes a problem,
swap the storage backing this class for a shared cache — the
`DashboardCache` interface (get_or_build / invalidate) doesn't need to
change.

Framework-free and independently testable, same as every other pure
module in this codebase (`state_diff`, `graph_metrics`, etc.).
"""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

_Builder = Callable[[], Awaitable[Any]]


class DashboardCache:
    """Keyed TTL cache. Each key gets its own expiry, so `/dashboard/
    events` (short TTL — it's a live feed) and `/dashboard/versions`
    (long TTL — versions don't change every second) can share one
    cache instance with different freshness needs."""

    def __init__(self, default_ttl_seconds: float = 3.0) -> None:
        self.default_ttl_seconds = default_ttl_seconds
        self._store: Dict[str, Tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any, ttl_seconds: Optional[float] = None) -> None:
        ttl = self.default_ttl_seconds if ttl_seconds is None else ttl_seconds
        self._store[key] = (time.monotonic() + ttl, value)

    def invalidate(self, key: Optional[str] = None) -> None:
        """Drops one key, or the whole cache if `key` is omitted —
        useful right after an action that should be reflected
        immediately (e.g. a workflow version was just registered)."""
        if key is None:
            self._store.clear()
        else:
            self._store.pop(key, None)

    async def get_or_build(self, key: str, builder: _Builder, ttl_seconds: Optional[float] = None) -> Any:
        """The main entry point: return the cached value if fresh,
        otherwise await `builder()`, cache it, and return it. `builder`
        is only ever called on a miss — a slow aggregation behind a
        cache hit costs nothing."""
        cached = self.get(key)
        if cached is not None:
            return cached
        value = await builder()
        self.set(key, value, ttl_seconds)
        return value

    def __len__(self) -> int:
        return len(self._store)


# Process-wide default instance. `dashboard_service.py` uses this by
# default but accepts an injected cache, so tests (and any future
# multi-tenant deployment that wants isolated caches) aren't stuck
# sharing global state.
default_cache = DashboardCache()
