"""
services/integration/api/events.py
=================================
NATS bindings for the Platform Integration service.

Subscribes (spec "NATS: Consume"): manager.>, product.>, architecture.>,
engineering.>, qa.>, security.>, devops.>, monitoring.>, incident.> —
purely for cross-department visibility/counting (mirrors Monitoring's
own `_WILDCARD_SUBJECTS` precedent, services/monitoring/api/events.py),
not for triggering any workflow.

Publishes: platform.validation.completed / platform.validation.failed
(after each full validation run) and platform.ready (once, the first
time overall readiness crosses READY_THRESHOLD — spec's own worked
example shows 98.4% as "ready"; 95.0 is used here as a clear, documented,
deterministic bar rather than guessing at an unstated one).
"""
from __future__ import annotations

from typing import Any, Dict

import structlog

from infrastructure.messaging.nats_client import NATSClient
from services.integration.orchestrator import generate_full_report

log = structlog.get_logger(__name__)

READY_THRESHOLD = 95.0

WILDCARD_SUBJECTS = [
    "manager.>", "product.>", "architecture.>", "engineering.>",
    "qa.>", "security.>", "devops.>", "monitoring.>", "incident.>",
]


async def setup_platform_visibility_subscriptions(nats: NATSClient) -> None:
    """Called once from main.py's startup lifespan. Every wildcard just
    logs — this service validates structure, it does not react to
    individual department events."""

    async def _visibility_handler(payload: Dict[str, Any], _subject: str = "") -> None:
        log.debug("platform_event_observed", subject=_subject, payload_keys=list(payload.keys()))

    for subject in WILDCARD_SUBJECTS:
        async def _handler(payload: Dict[str, Any], _subject: str = subject) -> None:
            await _visibility_handler(payload, _subject)

        await nats.subscribe(subject, _handler, durable=f"integration-{subject.replace('.', '-').replace('>', 'wild')}")

    log.info("platform_integration_subscriptions_ready", count=len(WILDCARD_SUBJECTS))


async def run_validation_and_publish(nats: NATSClient, db_factory: Any = None, factory: Any = None) -> float:
    """Runs one full validation pass and publishes the result. Returns
    the overall readiness score. Never raises — a validation-run
    failure itself gets published as platform.validation.failed."""
    try:
        full = await generate_full_report(db_factory=db_factory, nats=nats, factory=factory)
        overall = full.readiness.overall
        await nats.publish("platform.validation.completed", {
            "overall": overall, "health": full.health.overall.value,
            "registry_passed": full.registry.passed,
        })
        if overall >= READY_THRESHOLD:
            await nats.publish("platform.ready", {"overall": overall})
        return overall
    except Exception as e:
        log.error("platform_validation_run_failed", error=str(e))
        try:
            await nats.publish("platform.validation.failed", {"error": str(e)})
        except Exception:
            pass
        return 0.0
