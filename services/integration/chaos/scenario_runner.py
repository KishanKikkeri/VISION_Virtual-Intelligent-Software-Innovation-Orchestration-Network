"""
services/integration/chaos/scenario_runner.py
=================================
M4.5 §5 Scenario Runner — the one place the brief's diagram (inject
faults → execute workflow → collect metrics → validate recovery →
generate report) actually happens, wiring together
`fault_injector.apply_fault` (§4), `recovery_validator.
build_recovery_signal` (§6), and M4.4's own `benchmark_runner.
atime_call` for timing (§13 Benchmark Integration — literally calling
that function rather than re-timing things itself, per "do not
duplicate benchmarking code").

**Independence between scenarios** (brief's "scenarios must never
depend on previous executions"): `run_scenario` takes every fault spec
and callable fresh as arguments and holds no module-level mutable
state — two calls to `run_scenario` in sequence, or concurrently via
`asyncio.gather`, share nothing (each fault-wrapping closure in
`fault_injector.py` is freshly created per call, per that module's own
docstring). A caller that wants to run a full suite of scenarios calls
`run_scenario` once per scenario and assembles the results into a
`ChaosRun` itself (see `run_chaos_suite` below, which does exactly
that and nothing more).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from services.integration.benchmarking import benchmark_runner
from services.integration.chaos import fault_injector, recovery_validator
from services.integration.chaos.chaos_models import ChaosRun, FaultSpec, ScenarioResult

AsyncCallable = Callable[[], Awaitable[Any]]
AsyncBoolCheck = Callable[[], Awaitable[bool]]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def run_scenario(
    scenario_name: str,
    workflow_fn: AsyncCallable,
    faults: Optional[List[FaultSpec]] = None,
    max_retries: int = 0,
    recovery_checks: Optional[Dict[str, AsyncBoolCheck]] = None,
    baseline_execution_ms: Optional[float] = None,
) -> ScenarioResult:
    """Runs one independent scenario. `workflow_fn` is the real
    interface being chaos-tested (a zero-arg async callable — the
    caller closes over whatever arguments the underlying workflow/
    agent/repository call actually needs, same convention
    `benchmark_runner.py` uses); `faults` are applied to it in order
    via `fault_injector.apply_fault` before execution.

    `max_retries` (default 0 — no retry) re-invokes the *fault-wrapped*
    callable up to `max_retries` additional times on failure, recording
    a True/False per attempt for `recovery_validator.validate_retries`
    — this is what makes "retry success" (§6) measurable rather than
    assumed; the retries happen against the same faults (a fault that
    always triggers will still fail every retry, which is realistic:
    retrying against a database that's genuinely down doesn't help
    without the fault itself resolving, which `FaultSpec.probability`
    below 1.0 is how a scenario models "recovers after N attempts").

    `recovery_checks`, if given, maps signal names ("rollback",
    "incident", "alert", "dlq", "workflow_completion", "checkpoint")
    to the async check functions `recovery_validator.
    build_recovery_signal` expects — see that function's docstring.
    """
    started_at = _now_iso()
    wrapped = workflow_fn
    fault_events = []
    for spec in (faults or []):
        wrapped, event = fault_injector.apply_fault(wrapped, spec)
        fault_events.append(event)

    attempts: List[bool] = []
    exec_ms: Optional[float] = None
    last_error: Optional[BaseException] = None
    for _ in range(max_retries + 1):
        try:
            _, ms = await benchmark_runner.atime_call(wrapped)
            exec_ms = ms if exec_ms is None else exec_ms + ms
            attempts.append(True)
            last_error = None
            break
        except Exception as e:  # noqa: BLE001 — a failed attempt is data (an attempt result), not a crashed run
            attempts.append(False)
            last_error = e

    ended_at = _now_iso()
    success = bool(attempts) and attempts[-1]

    checks = recovery_checks or {}
    recovery = await recovery_validator.build_recovery_signal(
        retry_attempts=attempts,
        fault_injected_at=fault_events[0].injected_at if fault_events else started_at,
        recovered_at=ended_at if success else None,
        rollback_check=checks.get("rollback"),
        incident_check=checks.get("incident"),
        alert_check=checks.get("alert"),
        dlq_check=checks.get("dlq"),
        workflow_completion_check=checks.get("workflow_completion"),
        checkpoint_check=checks.get("checkpoint"),
    )

    duration_ms = recovery_validator.compute_recovery_latency_ms(started_at, ended_at) or 0.0

    benchmark: Dict[str, Any] = {"execution_time_ms": exec_ms, "attempts": len(attempts)}
    if baseline_execution_ms is not None and exec_ms is not None and baseline_execution_ms > 0:
        benchmark["performance_degradation_pct"] = ((exec_ms - baseline_execution_ms) / baseline_execution_ms) * 100.0

    notes = str(last_error) if (last_error is not None and not success) else None

    return ScenarioResult(
        scenario_name=scenario_name, faults=fault_events, recovery=recovery, success=success,
        started_at=started_at, ended_at=ended_at, duration_ms=duration_ms, benchmark=benchmark, notes=notes,
    )


async def run_chaos_suite(
    name: str,
    scenarios: List[Dict[str, Any]],
    version: str,
    platform_version: Optional[str] = None,
    benchmark_version: Optional[str] = None,
    workflow_version: Optional[str] = None,
    commit_hash: Optional[str] = None,
    environment: str = "unknown",
) -> ChaosRun:
    """Runs every entry in `scenarios` (each a kwargs dict for
    `run_scenario` — `{"scenario_name": ..., "workflow_fn": ...,
    "faults": [...], ...}`) independently and assembles the results
    into one `ChaosRun`. Version fields are the brief's §14 pass-
    throughs (see `chaos_models.ChaosRun`'s docstring) — this function
    does not compute or look them up itself."""
    from services.integration.chaos import resilience_analyzer

    results: List[ScenarioResult] = []
    for spec in scenarios:
        results.append(await run_scenario(**spec))

    metrics = resilience_analyzer.compute_resilience_metrics(results)

    return ChaosRun(
        name=name, version=version, timestamp=_now_iso(),
        platform_version=platform_version, benchmark_version=benchmark_version,
        workflow_version=workflow_version, commit_hash=commit_hash, environment=environment,
        scenarios=results, metrics=metrics,
    )
