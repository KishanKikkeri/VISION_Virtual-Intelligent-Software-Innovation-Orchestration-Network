"""
services/devops/api/events.py
=================================
NATS event bindings for DevOps Service.

Subscribes:
  qa.phase.completed          -- one half of the Stage A trigger
  security.phase.completed    -- the other half; DevOps waits for BOTH
                                  before generating a deployment plan
                                  (mirrors Manager's own validation_phase
                                  fan-out barrier)

Publishes (mirrors what DevOpsHead's AgentResult already queues):
  devops.phase.started      deployment.started      deployment.completed
  deployment.failed         rollback.completed       health.completed
  devops.phase.completed

(Messaging cleanup, see docs/M3.9_Platform_Integration_Handover.md §6:
this file used to also subscribe to `manager.deploy.approved`, documented
as a "standalone-mode equivalent" of Manager's synchronous in-process
call — but nothing in the platform ever actually publishes that event,
so the subscription was permanently dead. Removed rather than
half-implementing the standalone-publish side of that duality, which is
a bigger change than a messaging cleanup pass warrants; if a genuinely
decoupled DevOps deployment mode is wanted later, both the publish side
in Manager and this subscription should be reintroduced together.)
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Set

import structlog

from infrastructure.messaging.nats_client import NATSClient

log = structlog.get_logger(__name__)

DevOpsRunner = Callable[[str, str, str], Any]   # (project_id, workflow_id, feature_name) -> Awaitable

# Tracks which of {"qa", "security"} have completed per project, so
# Stage A only fires once BOTH gates have passed — same barrier pattern
# Manager's validation_phase_node already uses.
_pending_validation: Dict[str, Set[str]] = {}


async def _on_gate_completed(gate: str, runner: Optional[DevOpsRunner], payload: Dict[str, Any]) -> None:
    project_id = payload.get("project_id")
    if not project_id:
        return
    passed = payload.get("passed", payload.get("verdict") != "fail")
    if not passed:
        log.info("devops_gate_not_passed", gate=gate, project_id=project_id)
        return

    seen = _pending_validation.setdefault(project_id, set())
    seen.add(gate)
    if {"qa", "security"} <= seen:
        _pending_validation.pop(project_id, None)
        if runner is None:
            log.warning("devops_runner_not_configured", project_id=project_id)
            return
        workflow_id = payload.get("workflow_id", "unknown")
        feature_name = payload.get("feature_name", "default")
        log.info("devops_auto_start", project_id=project_id, feature_name=feature_name)
        await runner(project_id, workflow_id, feature_name)


async def setup_devops_subscriptions(nats: NATSClient, runner: Optional[DevOpsRunner] = None) -> None:
    """Called once from main.py's startup lifespan."""

    async def qa_completed_handler(payload: Dict[str, Any]) -> None:
        await _on_gate_completed("qa", runner, payload)

    async def security_completed_handler(payload: Dict[str, Any]) -> None:
        await _on_gate_completed("security", runner, payload)

    await nats.subscribe("qa.phase.completed", qa_completed_handler, durable="devops-qa-completed")
    await nats.subscribe("security.phase.completed", security_completed_handler, durable="devops-security-completed")
    log.info("devops_subscriptions_ready")
