"""
services/qa/api/events.py
============================
NATS event bindings for QA Service.

Subscribes:
  engineering.phase.completed  -> auto-starts the QA pipeline once
                                   Engineering hands off a merge-ready PR
  engineering.retry.completed  -> re-runs QA after Engineering addresses
                                   a previous DefectReport

Publishes (mirrors what QAHead's AgentResult already queues, so this is
the standalone-service equivalent of that in-process path):
  qa.phase.started      qa.test.generated       qa.execution.completed
  qa.coverage.completed qa.defect.created       qa.retry.requested
  qa.phase.completed    qa.phase.failed
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

import structlog

from infrastructure.messaging.nats_client import NATSClient

log = structlog.get_logger(__name__)

QARunner = Callable[[str, str, str], Any]   # (project_id, workflow_id, feature_name) -> Awaitable


async def _on_engineering_completed(runner: Optional[QARunner], payload: Dict[str, Any]) -> None:
    project_id = payload.get("project_id")
    workflow_id = payload.get("workflow_id", "unknown")
    feature_name = payload.get("feature_name", "default")

    if runner is None:
        log.warning("qa_runner_not_configured", project_id=project_id)
        return

    log.info("qa_auto_start", project_id=project_id, feature_name=feature_name)
    await runner(project_id, workflow_id, feature_name)


async def _on_engineering_retry_completed(runner: Optional[QARunner], payload: Dict[str, Any]) -> None:
    """QA re-validates once Engineering has addressed a prior DefectReport."""
    await _on_engineering_completed(runner, payload)


async def setup_qa_subscriptions(nats: NATSClient, runner: Optional[QARunner] = None) -> None:
    """Called once from main.py's startup lifespan."""

    async def eng_completed_handler(payload: Dict[str, Any]) -> None:
        await _on_engineering_completed(runner, payload)

    async def eng_retry_handler(payload: Dict[str, Any]) -> None:
        await _on_engineering_retry_completed(runner, payload)

    await nats.subscribe(
        "engineering.phase.completed", eng_completed_handler,
        durable="qa-engineering-completed",
    )
    await nats.subscribe(
        "engineering.retry.completed", eng_retry_handler,
        durable="qa-engineering-retry-completed",
    )
    log.info("qa_subscriptions_ready")
