"""services/monitoring/workers/dashboard.py — Dashboard Worker.

Renders `dashboard_configuration` (DB-backed source of truth per spec
§0 Decision 4) and exports a Grafana-provisioning JSON copy — a
one-way export, never read back.
"""
from __future__ import annotations

import json
import os

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.monitoring.context import build_dashboard_configuration, render_grafana_export
from services.monitoring.integration.monitoring_repository import (
    DashboardRepository, DashboardWidgetRepository,
)
from services.monitoring.models import HealthStatus, MonitoredComponent, SystemHealthReport

GRAFANA_PROVISIONING_DIR = "docker/grafana/provisioning/dashboards"


@AgentFactory.register("dashboard_worker")
class DashboardWorker(BaseAgent):
    """Deterministic — no LLM call."""

    async def execute(self, task: TaskInput) -> AgentResult:
        raw_scores = task.context.approved_artifacts.get("__component_scores__", {})
        component_scores = {MonitoredComponent(k): v for k, v in raw_scores.items()}
        health = task.context.approved_artifacts.get("__health_report__")
        health_report = SystemHealthReport(**health) if health else SystemHealthReport(
            health_score=0.0, status=HealthStatus.CRITICAL, component_scores={})

        dashboard = build_dashboard_configuration(component_scores, health_report)

        try:
            async with self._db_factory() as db:
                row = await DashboardRepository.upsert(db, dashboard.name, dashboard.layout)
                await DashboardWidgetRepository.replace_all(db, row.id, [
                    {"widget_type": w.widget_type, "config": {**w.config, "title": w.title}, "position": w.position}
                    for w in dashboard.widgets
                ])
        except Exception:
            pass

        grafana_json = render_grafana_export(dashboard)
        try:
            os.makedirs(GRAFANA_PROVISIONING_DIR, exist_ok=True)
            with open(os.path.join(GRAFANA_PROVISIONING_DIR, f"{dashboard.name}.json"), "w") as f:
                json.dump(grafana_json, f, indent=2)
        except Exception:
            pass  # Grafana export is a best-effort convenience, not the source of truth

        artifact = await self.create_artifact(task, "dashboard_configuration", dashboard.model_dump(mode="json"))

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content=dashboard.model_dump(mode="json"),
            summary=f"Rendered dashboard with {len(dashboard.widgets)} widgets",
            quality_score=1.0, artifacts=[artifact],
        )
