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
  manager.deploy.approved     -- standalone-mode equivalent of Manager's
                                  synchronous `_run_department_pipeline`
                                  call in services/manager/main.py's
                                  `deployment_plan` branch (see that
                                  module's docstring for why the
                                  in-process runtime doesn't also publish
                                  this event: the direct call already
                                  covers it, exactly like QA/Security's
                                  own NATS-vs-in-process duality)

Publishes (mirrors what DevOpsHead's AgentResult already queues):
  devops.phase.started      deployment.started      deployment.completed
  deployment.failed         rollback.completed       health.completed
  devops.phase.completed
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Set

import structlog

from infrastructure.messaging.nats_client import NATSClient

log = structlog.get_logger(__name__)

DevOpsRunner = Callable[[str, str, str], Any]   # (project_id, workflow_id, feature_name) -> Awaitable
ApprovedRunner = Callable[[str], Any]           # (project_id) -> Awaitable

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


async def _on_deploy_approved(runner: Optional[ApprovedRunner], payload: Dict[str, Any]) -> None:
    project_id = payload.get("project_id")
    if not project_id:
        return
    if runner is None:
        log.warning("devops_approved_runner_not_configured", project_id=project_id)
        return
    await runner(project_id)


async def setup_devops_subscriptions(nats: NATSClient, runner: Optional[DevOpsRunner] = None,
                                      approved_runner: Optional[ApprovedRunner] = None) -> None:
    """Called once from main.py's startup lifespan."""

    async def qa_completed_handler(payload: Dict[str, Any]) -> None:
        await _on_gate_completed("qa", runner, payload)

    async def security_completed_handler(payload: Dict[str, Any]) -> None:
        await _on_gate_completed("security", runner, payload)

    async def deploy_approved_handler(payload: Dict[str, Any]) -> None:
        await _on_deploy_approved(approved_runner, payload)

    await nats.subscribe("qa.phase.completed", qa_completed_handler, durable="devops-qa-completed")
    await nats.subscribe("security.phase.completed", security_completed_handler, durable="devops-security-completed")
    await nats.subscribe("manager.deploy.approved", deploy_approved_handler, durable="devops-deploy-approved")
    log.info("devops_subscriptions_ready")
