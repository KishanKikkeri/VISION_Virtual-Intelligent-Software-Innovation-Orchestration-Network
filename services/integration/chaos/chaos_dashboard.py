"""
services/integration/chaos/chaos_dashboard.py
=================================
M4.5 §12 "Dashboard Integration" — "reuse existing dashboard
infrastructure; do not create a separate UI." This module does not
render anything itself: it's the one function
(`fetch_chaos_dashboard_section`) `services/integration/dashboard/
dashboard_service.py` calls, following the exact same `_fetch_*`
degradation convention every other data source in that file already
uses (missing/unwired dependency degrades this one card, not the
whole dashboard) — see that module's docstring.

**Running scenarios / active faults** (brief's two "live" fields) are
process-local, in-memory state via `ActiveRunTracker` below —
`scenario_runner.run_scenario` has no module-level mutable state by
design (its own docstring: "scenarios must never depend on previous
executions"), so this tracker is intentionally a separate, optional
add-on a caller can wrap around `run_scenario` calls (see
`track_scenario`) rather than something `scenario_runner.py` itself
needs to import — a chaos run executed without wrapping it in
`track_scenario` still works identically; it just won't show up as
"currently running" on the dashboard while in flight (it will still
show up in history once persisted via `chaos_repository.py`).

**Latest resilience score / historical trend** come from
`chaos_repository.py` when a `db` is available, same as every other
dashboard card that needs history.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar

import structlog

log = structlog.get_logger(__name__)

T = TypeVar("T")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ActiveRunTracker:
    """Process-local registry of in-flight scenario executions —
    same "process-local by design, durable history is the repository's
    job" rationale `benchmark_registry.BenchmarkRegistry`'s docstring
    gives for its own in-memory store."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: Dict[str, Dict[str, Any]] = {}

    def start(self, run_key: str, scenario_name: str, fault_targets: List[str]) -> None:
        with self._lock:
            self._running[run_key] = {
                "scenario_name": scenario_name, "started_at": _now_iso(), "fault_targets": fault_targets,
            }

    def finish(self, run_key: str) -> None:
        with self._lock:
            self._running.pop(run_key, None)

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [{"run_key": k, **v} for k, v in self._running.items()]


default_tracker = ActiveRunTracker()


async def track_scenario(
    run_key: str, scenario_name: str, fault_targets: List[str],
    coro_fn: Callable[[], Awaitable[T]], tracker: ActiveRunTracker = default_tracker,
) -> T:
    """Thin wrapper: registers `scenario_name` as running in `tracker`
    before awaiting `coro_fn()` (typically a `scenario_runner.
    run_scenario` call), and always deregisters it afterward — a
    caller that wants "currently running" to show up on the dashboard
    wraps its `run_scenario` call in this; a caller that doesn't care
    calls `run_scenario` directly."""
    tracker.start(run_key, scenario_name, fault_targets)
    try:
        return await coro_fn()
    finally:
        tracker.finish(run_key)


async def fetch_chaos_dashboard_section(
    db: Any = None, name: str = "default", trend_limit: int = 20, tracker: ActiveRunTracker = default_tracker,
) -> Optional[Dict[str, Any]]:
    """The one function `dashboard_service._fetch_chaos` (once wired
    in — see `docs/M4.5_Chaos_Testing_Handover.md` §3) needs to call.
    Returns `None` only if the repository call itself raises (no chaos
    tables/DB configured yet) — an empty history is NOT `None`, it's a
    normal "no chaos runs yet" payload, same "missing infra vs. no
    activity yet" distinction `dashboard_service.py` draws for its own
    sources."""
    running = tracker.snapshot()
    active_faults = sorted({t for r in running for t in r.get("fault_targets", [])})

    latest_score: Optional[float] = None
    trend: List[Dict[str, Any]] = []
    if db is not None:
        try:
            from services.integration.chaos.chaos_repository import ChaosRepository

            latest = await ChaosRepository.get_latest(db, name)
            if latest is not None:
                from services.integration.chaos import resilience_analyzer
                latest_score = resilience_analyzer.compute_resilience_score(latest.run.metrics)

            history = await ChaosRepository.list_history(db, name, limit=trend_limit)
            from services.integration.chaos import resilience_analyzer as _ra
            trend = [
                {"timestamp": r.run.timestamp, "version": r.run.version,
                 "resilience_score": _ra.compute_resilience_score(r.run.metrics)}
                for r in history
            ]
        except Exception as e:  # noqa: BLE001 — degrades this card, not the whole dashboard
            log.info("chaos_dashboard_history_unavailable", error=str(e))
            return None

    return {
        "running_scenarios": running,
        "active_faults": active_faults,
        "latest_resilience_score": latest_score,
        "historical_trend": trend,
    }
