"""
services/integration/chaos/fault_injector.py
=================================
M4.5 §4 Fault Injection Rules — "faults must wrap existing interfaces
instead of modifying implementations" and "never patch production
code," implemented literally: every function here takes a callable
(the real interface — a repository method, an agent call, an API
client call, supplied by the caller, same isolation principle
`benchmark_runner.py` uses for measurement) and returns a **new**
wrapped callable plus the `FaultEvent` describing what will happen
when it's called. Nothing here uses monkeypatching, `unittest.mock.
patch`, import-hook rewriting, or any other mechanism that would touch
the original interface's module — the original callable is completely
untouched and usable normally by anything that isn't holding the
wrapped version.

Determinism (brief's §17): every wrapper takes an explicit
`probability` (default 1.0 — always triggers) rather than reaching for
a hidden global random seed, so a scenario built with `probability=1.0`
faults is fully deterministic across CI runs; `probability<1.0` is
opt-in randomness for the two fault types that are inherently
probabilistic (`RANDOM_EXCEPTION`, `PARTIAL_WORKFLOW_FAILURE`).

Cleanup (brief's §17 "every injected fault has a clear cleanup path"):
because a wrapped callable is a pure new object that never mutates the
original, "cleanup" for every fault type in this module is simply
"stop calling the wrapped version, call the original" — there is no
teardown step to forget. The one fault type where this module's
wrapping approach is **not** sufficient on its own —
`DOCKER_FAILURE`, which the brief lists as a fault type but which (to
actually simulate) needs a real Docker/container-runtime handle this
slice does not have — is documented in `inject_docker_failure`'s own
docstring rather than silently faked.
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, Tuple, Type

from services.integration.chaos.chaos_models import FaultEvent, FaultSpec, FaultType

AsyncCallable = Callable[..., Awaitable[Any]]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rolls_triggered(probability: float) -> bool:
    """A `probability` of 1.0 (the common/CI-deterministic case) never
    calls `random` at all — avoids the appearance of nondeterminism
    even under a fixed seed for the default, always-trigger case."""
    if probability >= 1.0:
        return True
    if probability <= 0.0:
        return False
    return random.random() < probability


def _make_event(spec: FaultSpec, triggered: bool, duration_ms: Optional[float] = None,
                 error_message: Optional[str] = None) -> FaultEvent:
    return FaultEvent(
        fault_type=spec.fault_type, target=spec.target, injected_at=_now_iso(),
        triggered=triggered, duration_ms=duration_ms, error_message=error_message,
    )


class ChaosInjectedError(RuntimeError):
    """Raised by a fault-wrapped callable to signal a simulated
    failure — distinct from any exception type the real interface
    might itself raise, so a scenario's error handling can tell "the
    chaos framework did this on purpose" apart from "the wrapped call
    hit a genuine bug." Carries the `FaultType` that caused it."""
    def __init__(self, fault_type: FaultType, message: str) -> None:
        super().__init__(message)
        self.fault_type = fault_type


def inject_unavailable(fn: AsyncCallable, spec: FaultSpec) -> Tuple[AsyncCallable, FaultEvent]:
    """Wraps `fn` so every call raises `ChaosInjectedError` instead of
    running `fn` — used for `DATABASE_UNAVAILABLE`, `NATS_UNAVAILABLE`,
    `QDRANT_UNAVAILABLE`, and `REPOSITORY_FAILURE` (the brief's four
    "component is simply not there" faults, which all reduce to the
    same wrapping shape)."""
    triggered = _rolls_triggered(spec.probability)
    message = spec.exception_message or f"{spec.target} unavailable (chaos-injected: {spec.fault_type.value})"
    event = _make_event(spec, triggered, error_message=message if triggered else None)

    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        if triggered:
            raise ChaosInjectedError(spec.fault_type, message)
        return await fn(*args, **kwargs)

    return wrapped, event


def inject_latency(fn: AsyncCallable, spec: FaultSpec) -> Tuple[AsyncCallable, FaultEvent]:
    """Wraps `fn` so every call sleeps `spec.delay_ms` before running
    it — used for `SLOW_DATABASE`, `SLOW_API`, `SLOW_AGENT`. Requires
    `spec.delay_ms`; raises `ValueError` at wrap time (not at call
    time) if it's missing, so a misconfigured scenario fails fast."""
    if spec.delay_ms is None:
        raise ValueError(f"{spec.fault_type.value} requires delay_ms")
    triggered = _rolls_triggered(spec.probability)
    event = _make_event(spec, triggered, duration_ms=spec.delay_ms if triggered else None)

    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        if triggered:
            await asyncio.sleep(spec.delay_ms / 1000.0)
        return await fn(*args, **kwargs)

    return wrapped, event


def inject_timeout(fn: AsyncCallable, spec: FaultSpec, timeout_ms: float = 1.0) -> Tuple[AsyncCallable, FaultEvent]:
    """`LLM_TIMEOUT` — wraps `fn` with an `asyncio.wait_for` timeout so
    short that it always fires, surfacing as `asyncio.TimeoutError`
    (not `ChaosInjectedError`, since a real caller's timeout-handling
    code should see the same exception type a genuine LLM-call timeout
    would raise — this is the one fault type where matching the real
    exception shape matters more than the "clearly chaos-injected"
    convention the others use)."""
    triggered = _rolls_triggered(spec.probability)
    event = _make_event(spec, triggered, duration_ms=timeout_ms if triggered else None)

    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        if triggered:
            return await asyncio.wait_for(_never_completes(), timeout=timeout_ms / 1000.0)
        return await fn(*args, **kwargs)

    return wrapped, event


async def _never_completes() -> Any:
    await asyncio.sleep(3600)


def inject_rate_limit(fn: AsyncCallable, spec: FaultSpec) -> Tuple[AsyncCallable, FaultEvent]:
    """`LLM_RATE_LIMITING` — wraps `fn` to raise `ChaosInjectedError`
    with a 429-shaped message on the first `fail_count` calls (from
    `spec.metadata["fail_count"]`, default 1), then delegate to `fn`
    normally — simulates a rate limiter that recovers after a short
    backoff, which is the scenario recovery-validation (§6 "retry
    success") is meant to exercise against."""
    fail_count = int(spec.metadata.get("fail_count", 1))
    triggered = _rolls_triggered(spec.probability)
    message = spec.exception_message or f"{spec.target} rate limited (429, chaos-injected)"
    event = _make_event(spec, triggered, error_message=message if triggered else None)
    calls = {"n": 0}

    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if triggered and calls["n"] <= fail_count:
            raise ChaosInjectedError(spec.fault_type, message)
        return await fn(*args, **kwargs)

    return wrapped, event


def inject_random_exception(fn: AsyncCallable, spec: FaultSpec,
                             exception_cls: Type[BaseException] = RuntimeError) -> Tuple[AsyncCallable, FaultEvent]:
    """`RANDOM_EXCEPTION` — the one fault type that's explicitly
    probabilistic per call (not just "will this scenario's fault
    trigger at all," like every other wrapper here, but "does *this
    particular* call fail") — each invocation independently rolls
    `spec.probability`, so a long-running scenario sees a genuine
    mix of successes and failures rather than one all-or-nothing
    decision made at wrap time."""
    message = spec.exception_message or f"random exception injected for {spec.target} (chaos-injected)"
    event = _make_event(spec, triggered=True)  # "triggered" here means "the fault is active", not "always fails"

    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        if _rolls_triggered(spec.probability):
            raise exception_cls(message)
        return await fn(*args, **kwargs)

    return wrapped, event


def inject_partial_workflow_failure(fn: AsyncCallable, spec: FaultSpec) -> Tuple[AsyncCallable, FaultEvent]:
    """`PARTIAL_WORKFLOW_FAILURE` — wraps one workflow *step* callable
    (the caller picks which step; this module has no knowledge of any
    workflow's structure, per the isolation principle) so it fails on
    calls whose 1-indexed position is in `spec.metadata["fail_at_steps"]`
    (default: `[1]`, i.e. fail the first call) — simulating a workflow
    that fails partway through rather than at the very first or every
    step, which is what distinguishes this from `inject_unavailable`."""
    fail_at_steps = set(spec.metadata.get("fail_at_steps", [1]))
    message = spec.exception_message or f"partial workflow failure injected for {spec.target} (chaos-injected)"
    event = _make_event(spec, triggered=True)
    calls = {"n": 0}

    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] in fail_at_steps:
            raise ChaosInjectedError(spec.fault_type, message)
        return await fn(*args, **kwargs)

    return wrapped, event


def inject_websocket_disconnect(fn: AsyncCallable, spec: FaultSpec) -> Tuple[AsyncCallable, FaultEvent]:
    """`WEBSOCKET_DISCONNECT` — wraps a caller-supplied "send/receive
    over the socket" callable to raise `ConnectionResetError` instead,
    simulating a dropped connection mid-call (rather than the socket
    simply never being reachable, which `inject_unavailable` already
    covers for the "unavailable" family)."""
    triggered = _rolls_triggered(spec.probability)
    message = spec.exception_message or f"{spec.target} websocket disconnected (chaos-injected)"
    event = _make_event(spec, triggered, error_message=message if triggered else None)

    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        if triggered:
            raise ConnectionResetError(message)
        return await fn(*args, **kwargs)

    return wrapped, event


def inject_docker_failure(fn: AsyncCallable, spec: FaultSpec) -> Tuple[AsyncCallable, FaultEvent]:
    """`DOCKER_FAILURE` — **the one fault type this module cannot
    genuinely simulate without a real container-runtime handle.** What
    this wrapper *can* honestly do is the same as `inject_unavailable`
    (raise in place of calling `fn`, representing "the container this
    call depended on is gone") — which is what it does — but it cannot
    verify a real Docker daemon/container was involved, start/stop an
    actual container, or exercise any container-specific recovery path
    (e.g. a real restart policy). A caller that needs the latter needs
    to wire this scenario against `docker-py` or the platform's real
    container-orchestration layer directly; this wrapper is a
    same-shape stand-in, not a substitute, and callers should not treat
    a scenario using this wrapper as having validated real Docker
    resilience — see the module docstring's note on `DOCKER_FAILURE`
    and the handover doc's external-infrastructure caveats."""
    return inject_unavailable(fn, spec)


_INJECTORS = {
    FaultType.DATABASE_UNAVAILABLE: inject_unavailable,
    FaultType.NATS_UNAVAILABLE: inject_unavailable,
    FaultType.QDRANT_UNAVAILABLE: inject_unavailable,
    FaultType.REPOSITORY_FAILURE: inject_unavailable,
    FaultType.SLOW_DATABASE: inject_latency,
    FaultType.SLOW_API: inject_latency,
    FaultType.SLOW_AGENT: inject_latency,
    FaultType.LLM_TIMEOUT: inject_timeout,
    FaultType.LLM_RATE_LIMITING: inject_rate_limit,
    FaultType.RANDOM_EXCEPTION: inject_random_exception,
    FaultType.PARTIAL_WORKFLOW_FAILURE: inject_partial_workflow_failure,
    FaultType.WEBSOCKET_DISCONNECT: inject_websocket_disconnect,
    FaultType.DOCKER_FAILURE: inject_docker_failure,
}


def apply_fault(fn: AsyncCallable, spec: FaultSpec) -> Tuple[AsyncCallable, FaultEvent]:
    """Single entry point `scenario_runner.py` uses — dispatches to the
    right wrapper by `spec.fault_type` so callers don't need to know
    which of the functions above corresponds to which `FaultType`."""
    injector = _INJECTORS[spec.fault_type]
    return injector(fn, spec)
