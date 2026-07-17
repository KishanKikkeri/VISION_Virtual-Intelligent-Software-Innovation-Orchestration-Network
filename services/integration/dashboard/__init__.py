"""
services/integration/dashboard
=================================
M4.3 — Live Operations Dashboard. See dashboard_service.py for the
orchestration entry point (`build_platform_dashboard`), dashboard_
builder.py for the pure assembly functions, dashboard_repository.py
for the DB read layer, dashboard_cache.py for the TTL cache, and
dashboard_models.py for the shared Pydantic shapes.
"""
from services.integration.dashboard.dashboard_models import (
    DashboardSummary, EventStreamItem, IncidentSummary, MetricsSnapshot,
    PlatformDashboard, ServiceStatus, VersionSummary, WorkflowStatusEntry,
)
from services.integration.dashboard.dashboard_service import build_platform_dashboard

__all__ = [
    "DashboardSummary", "EventStreamItem", "IncidentSummary", "MetricsSnapshot",
    "PlatformDashboard", "ServiceStatus", "VersionSummary", "WorkflowStatusEntry",
    "build_platform_dashboard",
]
