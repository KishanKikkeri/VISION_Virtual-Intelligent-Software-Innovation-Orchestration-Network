"""
services/monitoring/agents — re-export shim for AgentFactory.

AgentFactory._load_class() imports "services.monitoring.agents" when
department == "monitoring" (see core/runtime/factory.py's dept_map).
The actual M3.7 hierarchical implementation lives in workers/, leads/,
and head/ (mirroring services/devops/agents exactly). Every concrete
class is registered directly via the @AgentFactory.register(...)
decorator at import time, so this module's only job is making sure
all ten classes are imported (triggering their decorators) and
re-exported for discoverability.
"""
from __future__ import annotations

from services.monitoring.head import MonitoringHead
from services.monitoring.leads import AlertingLead, MetricsLead, ObservabilityLead
from services.monitoring.workers import (
    AlertWorker,
    ApplicationMetricsWorker,
    DashboardWorker,
    InfrastructureMetricsWorker,
    LogAnalysisWorker,
    TraceAnalysisWorker,
)

__all__ = [
    "MonitoringHead",
    "MetricsLead", "ObservabilityLead", "AlertingLead",
    "InfrastructureMetricsWorker", "ApplicationMetricsWorker",
    "LogAnalysisWorker", "TraceAnalysisWorker",
    "AlertWorker", "DashboardWorker",
]
