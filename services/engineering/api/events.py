"""
services/engineering/api/events.py
=====================================
NATS event bindings for Engineering Service.

Subscribes:
  architecture.design.completed   -> auto-starts the Engineering pipeline
                                      once Architecture (including the
                                      Appendix A ui_blueprint) is approved

Publishes (mirrors what EngineeringHead's AgentResult already queues,
so this is the standalone-service equivalent of that in-process path):
  engineering.pipeline.started     engineering.plan.created
  engineering.modules.aggregated   engineering.tasks.dead_lettered
  engineering.pipeline.failed      engineering.phase.completed
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

import structlog

from infrastructure.messaging.nats_client import NATSClient

log = structlog.get_logger(__name__)

# Injected by main.py at startup — the callable that actually runs the
# Engineering pipeline (AgentFactory-driven or graph-driven).
EngineeringRunner = Callable[[str, str, str], Any]   # (project_id, workflow_id, feature_name) -> Awaitable


async def _on_architecture_completed(
    runner: Optional[EngineeringRunner], payload: Dict[str, Any],
) -> None:
    project_id  = payload.get("project_id")
    workflow_id = payload.get("workflow_id", "unknown")
    feature_name = payload.get("feature_name", "default")

    if payload.get("requires_approval", True):
        # Architecture may emit this event before human approval; Engineering
        # only starts once approval_status == approved is reflected in the
        # event, or on the follow-up completion event Manager Service sends.
        if not payload.get("approved", False):
            log.info("engineering_waiting_for_approval", project_id=project_id)
            return

    if runner is None:
        log.warning("engineering_runner_not_configured", project_id=project_id)
        return

    log.info("engineering_auto_start", project_id=project_id, feature_name=feature_name)
    await runner(project_id, workflow_id, feature_name)


async def setup_engineering_subscriptions(
    nats: NATSClient, runner: Optional[EngineeringRunner] = None,
) -> None:
    """Called once from main.py's startup lifespan."""

    async def arch_completed_handler(payload: Dict[str, Any]) -> None:
        await _on_architecture_completed(runner, payload)

    await nats.subscribe(
        "architecture.design.completed", arch_completed_handler,
        durable="engineering-architecture-completed",
    )
    log.info("engineering_subscriptions_ready")
