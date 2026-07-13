"""services/monitoring/workers/log_analysis.py — Log Analysis Worker.

Derives an error-rate signal from the platform's existing, real
`audit_events` table (every agent run already logs
`agent.<id>.completed|failed|escalated` there via BaseAgent._post_execute
— see core/runtime/base_agent.py). Monitoring does not stand up a
second log store; it reads the one that already exists.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.monitoring.integration.monitoring_repository import MonitoringLogRepository

LOOKBACK_MINUTES = 15


@AgentFactory.register("log_analysis_worker")
class LogAnalysisWorker(BaseAgent):
    """Deterministic — no LLM call."""

    async def execute(self, task: TaskInput) -> AgentResult:
        try:
            from infrastructure.database.models import AuditEvent

            cutoff = datetime.utcnow() - timedelta(minutes=LOOKBACK_MINUTES)
            async with self._db_factory() as db:
                result = await db.execute(
                    select(AuditEvent).where(AuditEvent.recorded_at >= cutoff))
                events = list(result.scalars().all())

            total = len(events)
            errors = sum(1 for e in events if e.event_type.endswith(".failed")
                         or e.event_type.endswith(".escalated"))
            error_rate = round(errors / total, 4) if total else 0.0

            if errors:
                async with self._db_factory() as db:
                    await MonitoringLogRepository.record(
                        db, service="platform", level="warning" if error_rate < 0.2 else "error",
                        message=f"{errors}/{total} agent runs failed or escalated in last "
                                f"{LOOKBACK_MINUTES}m",
                        context={"error_rate": error_rate, "window_minutes": LOOKBACK_MINUTES},
                    )

            return AgentResult(
                task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
                content={"error_rate": error_rate, "total_events": total, "error_events": errors},
                summary=f"error_rate={error_rate} over {total} audit events "
                        f"(last {LOOKBACK_MINUTES}m)",
                quality_score=1.0,
            )
        except Exception as e:
            return AgentResult(
                task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
                content={"error_rate": 0.0, "total_events": 0, "error_events": 0},
                summary=f"log analysis degraded: {e}",
                quality_score=0.5,
            )
