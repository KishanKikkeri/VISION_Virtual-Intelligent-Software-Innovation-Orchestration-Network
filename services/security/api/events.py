"""
services/security/api/events.py
===================================
NATS event bindings for Security Service.

Subscribes:
  engineering.phase.completed  -> auto-starts the Security pipeline once
                                   Engineering hands off a merge-ready PR
                                   (in parallel with QA — Security does
                                   NOT wait for qa.phase.completed)
  engineering.retry.completed  -> re-runs Security after Engineering
                                   addresses a previous SecurityFinding

Publishes (mirrors what SecurityHead's AgentResult already queues, so
this is the standalone-service equivalent of that in-process path):
  security.phase.started    security.scan.completed
  security.findings.created security.retry.requested
  security.phase.completed  security.phase.failed
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

import structlog

from infrastructure.messaging.nats_client import NATSClient

log = structlog.get_logger(__name__)

SecurityRunner = Callable[[str, str, str], Any]   # (project_id, workflow_id, feature_name) -> Awaitable


async def _on_engineering_completed(runner: Optional[SecurityRunner], payload: Dict[str, Any]) -> None:
    project_id = payload.get("project_id")
    workflow_id = payload.get("workflow_id", "unknown")
    feature_name = payload.get("feature_name", "default")

    if runner is None:
        log.warning("security_runner_not_configured", project_id=project_id)
        return

    log.info("security_auto_start", project_id=project_id, feature_name=feature_name)
    await runner(project_id, workflow_id, feature_name)


async def _on_engineering_retry_completed(runner: Optional[SecurityRunner], payload: Dict[str, Any]) -> None:
    """Security re-validates once Engineering has addressed a prior SecurityFinding."""
    await _on_engineering_completed(runner, payload)


async def setup_security_subscriptions(nats: NATSClient, runner: Optional[SecurityRunner] = None) -> None:
    """Called once from main.py's startup lifespan."""

    async def eng_completed_handler(payload: Dict[str, Any]) -> None:
        await _on_engineering_completed(runner, payload)

    async def eng_retry_handler(payload: Dict[str, Any]) -> None:
        await _on_engineering_retry_completed(runner, payload)

    await nats.subscribe(
        "engineering.phase.completed", eng_completed_handler,
        durable="security-engineering-completed",
    )
    await nats.subscribe(
        "engineering.retry.completed", eng_retry_handler,
        durable="security-engineering-retry-completed",
    )
    log.info("security_subscriptions_ready")
