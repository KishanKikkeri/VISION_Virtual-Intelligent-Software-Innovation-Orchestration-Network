"""
services/integration/plugin_sdk/sandbox/sandbox_monitor.py
=================================
M5.1 §"Monitoring" — "Track: execution duration, failures, crashes,
timeout count, permission violations, successful executions. Expose a
dashboard summary model." Pure in-memory counters, per plugin id, fed
by `SandboxManager.execute` after every run. Deliberately not
DB-backed itself (persistence is `sandbox_repository.py`'s job,
additive and separate) — this module is the same "cheap, in-process,
always-available" shape `plugin_registry.py`'s in-memory list is, so a
dashboard card can render even when no database is configured, per
this repository's established graceful-degradation convention.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from services.integration.plugin_sdk.sandbox.sandbox_models import (
    ExecutionPolicy, SandboxDashboardSummary, SandboxMetrics, SandboxResult,
)


class SandboxMonitor:
    def __init__(self) -> None:
        self._metrics: Dict[str, SandboxMetrics] = {}
        self._last_mode: ExecutionPolicy = ExecutionPolicy.NONE

    def record(self, mode: ExecutionPolicy, result: SandboxResult) -> None:
        self._last_mode = mode
        metrics = self._metrics.setdefault(result.plugin_id, SandboxMetrics(plugin_id=result.plugin_id))
        metrics.execution_count += 1
        metrics.total_duration_ms += result.duration_ms
        if result.success:
            metrics.success_count += 1
        else:
            metrics.failure_count += 1
        if result.timed_out:
            metrics.timeout_count += 1
        if result.crashed:
            metrics.crash_count += 1
        metrics.violation_count += len(result.violations)

    def metrics_for(self, plugin_id: str) -> Optional[SandboxMetrics]:
        return self._metrics.get(plugin_id)

    def all_metrics(self) -> List[SandboxMetrics]:
        return sorted(self._metrics.values(), key=lambda m: m.plugin_id)

    def dashboard_summary(self) -> SandboxDashboardSummary:
        all_metrics = self.all_metrics()
        total_executions = sum(m.execution_count for m in all_metrics)
        total_violations = sum(m.violation_count for m in all_metrics)
        total_timeouts = sum(m.timeout_count for m in all_metrics)
        total_failures = sum(m.failure_count for m in all_metrics)
        total_duration = sum(m.total_duration_ms for m in all_metrics)
        return SandboxDashboardSummary(
            healthy=(total_violations == 0 and total_timeouts == 0),
            execution_mode=self._last_mode,
            total_executions=total_executions,
            violation_count=total_violations,
            timeout_count=total_timeouts,
            failure_count=total_failures,
            average_runtime_ms=round(total_duration / total_executions, 2) if total_executions else 0.0,
        )

    def reset(self) -> None:
        self._metrics.clear()
        self._last_mode = ExecutionPolicy.NONE
