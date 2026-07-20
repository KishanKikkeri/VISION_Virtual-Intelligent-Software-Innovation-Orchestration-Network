"""
services/integration/chaos/recovery_validator.py
=================================
M4.5 §6 Recovery Validation — "validate that the platform behaves
correctly under failure... no manual inspection should be required."
Every function here is a pure assembler: it takes signals the caller
already has (a retry-attempt list, a rollback-check async callable, an
incident/alert lookup) and packages them into (or folds them onto) a
`RecoverySignal`. Nothing here polls a real incident-response service,
monitoring pipeline, or DLQ itself — those modules aren't in this
milestone's slice (same gap M4.4 documented for its own
department-callable dependencies) — so, per the isolation principle
established there, this module accepts the *result* of checking those
systems rather than checking them itself. A caller with the real
Incident/Monitoring/DLQ modules wires their check functions in here;
this module's job is only to fold the outcomes into one shape and
compute the one thing that's genuinely derivable without them:
recovery latency from a fault's injection time to a supplied recovery
timestamp.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Awaitable, Callable, List, NamedTuple, Optional

from services.integration.chaos.chaos_models import RecoverySignal

AsyncBoolCheck = Callable[[], Awaitable[bool]]


class RetryOutcome(NamedTuple):
    """Internal return shape for `validate_retries` — not persisted on
    its own; `RecoverySignal` is what a caller stores."""
    retried: bool
    retry_count: int
    recovered: bool


def validate_retries(attempts: List[bool]) -> RetryOutcome:
    """`attempts`: True/False per retry attempt, in order (True = that
    attempt succeeded). Returns `(retried, retry_count, recovered)` —
    `retried` is `len(attempts) > 0`; `recovered` is whether the
    *last* attempt succeeded (a workflow that retried 3 times and
    finally succeeded on the 3rd is "recovered"; one that exhausted
    all retries and still failed is not, even though it did retry)."""
    retried = len(attempts) > 0
    recovered = bool(attempts) and attempts[-1]
    return RetryOutcome(retried, len(attempts), recovered)


def compute_recovery_latency_ms(injected_at: str, recovered_at: str) -> Optional[float]:
    """Brief's "Recovery latency" metric — the one recovery signal
    genuinely derivable without an external system, given the two
    timestamps a scenario already has (`FaultEvent.injected_at` and
    whatever timestamp the caller's recovery check completed at).
    Returns `None` if either timestamp fails to parse, rather than
    raising — a malformed timestamp shouldn't crash an otherwise-valid
    scenario result."""
    try:
        start = datetime.fromisoformat(injected_at)
        end = datetime.fromisoformat(recovered_at)
    except (ValueError, TypeError):
        return None
    return max(0.0, (end - start).total_seconds() * 1000.0)


async def validate_rollback(check_fn: Optional[AsyncBoolCheck]) -> Optional[bool]:
    """Runs a caller-supplied async check (e.g. "was the transaction
    actually rolled back") and returns its boolean result, or `None`
    if no check was supplied — "not checked," never a guessed
    default."""
    if check_fn is None:
        return None
    return await check_fn()


async def validate_incident_generated(check_fn: Optional[AsyncBoolCheck]) -> Optional[bool]:
    """Same shape as `validate_rollback`, for the Incident Response
    service's "was an incident actually created for this fault"
    signal — the real check (querying that service) is the caller's
    to supply; M4's own Incident Response module isn't in this
    milestone's slice to call directly."""
    if check_fn is None:
        return None
    return await check_fn()


async def validate_alert_generated(check_fn: Optional[AsyncBoolCheck]) -> Optional[bool]:
    if check_fn is None:
        return None
    return await check_fn()


async def validate_dlq_routing(check_fn: Optional[AsyncBoolCheck]) -> Optional[bool]:
    if check_fn is None:
        return None
    return await check_fn()


async def validate_workflow_completion(check_fn: Optional[AsyncBoolCheck]) -> Optional[bool]:
    if check_fn is None:
        return None
    return await check_fn()


async def validate_checkpoint_recovery(check_fn: Optional[AsyncBoolCheck]) -> Optional[bool]:
    """Reuses M4.1's checkpoint/versioning concept: the caller's check
    function should confirm the workflow actually resumed from its
    last checkpoint rather than restarting from scratch — this module
    has no access to M4.1's checkpoint-migration code directly in this
    slice, so it only folds in the caller's answer."""
    if check_fn is None:
        return None
    return await check_fn()


async def build_recovery_signal(
    retry_attempts: Optional[List[bool]] = None,
    fault_injected_at: Optional[str] = None,
    recovered_at: Optional[str] = None,
    rollback_check: Optional[AsyncBoolCheck] = None,
    incident_check: Optional[AsyncBoolCheck] = None,
    alert_check: Optional[AsyncBoolCheck] = None,
    dlq_check: Optional[AsyncBoolCheck] = None,
    workflow_completion_check: Optional[AsyncBoolCheck] = None,
    checkpoint_check: Optional[AsyncBoolCheck] = None,
) -> RecoverySignal:
    """The one assembly point `scenario_runner.py` calls — folds every
    optional signal above into one `RecoverySignal`. Every argument is
    optional; a scenario that only cares about retries and ignores
    rollback/incident/alert/dlq/checkpoint checking gets a
    `RecoverySignal` with `None`s in those fields rather than an error
    for omitting them."""
    retried, retry_count, recovered = False, 0, False
    if retry_attempts is not None:
        retried, retry_count, recovered = validate_retries(retry_attempts)

    recovery_latency_ms = None
    if fault_injected_at is not None and recovered_at is not None:
        recovery_latency_ms = compute_recovery_latency_ms(fault_injected_at, recovered_at)

    return RecoverySignal(
        retried=retried, retry_count=retry_count, recovered=recovered,
        recovery_latency_ms=recovery_latency_ms,
        rollback_success=await validate_rollback(rollback_check),
        incident_generated=await validate_incident_generated(incident_check),
        alert_generated=await validate_alert_generated(alert_check),
        dlq_routed=await validate_dlq_routing(dlq_check),
        workflow_completed=await validate_workflow_completion(workflow_completion_check),
        checkpoint_recovered=await validate_checkpoint_recovery(checkpoint_check),
    )
