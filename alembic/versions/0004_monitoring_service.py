"""M3.7 Monitoring Service -- metrics, metric_samples, system_health, alerts,
alert_history, dashboards, dashboard_widgets, logs, traces, capacity_forecast.
Revision: 0004

NOTE (discovered during M3.8 reconnaissance, documented in
docs/M3.8_Incident_Response_Handover.md "known pre-existing issues"):
this migration did not exist prior to M3.8 even though M3.7 Monitoring's
ORM models were fully defined in infrastructure/database/models.py --
Monitoring's tables were never actually migrated. This file is a
genuine, additive bug fix (no Monitoring service code touched), added
because M3.8 Incident Response reads several of these tables
read-only (MonitoringProvider) and would otherwise be reading from
tables that don't exist in a fresh database.
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    from sqlalchemy import inspect
    from infrastructure.database.models import (
        Alert, AlertHistory, CapacityForecast, Dashboard, DashboardWidget,
        Metric, MetricSample, MonitoringLog, MonitoringTrace, SystemHealth,
    )

    bind = op.get_bind()
    inspector = inspect(bind)
    existing = inspector.get_table_names()

    # Parent tables first (metric_samples/alert_history/dashboard_widgets FK into their parents).
    for model in (
        Metric, MetricSample, SystemHealth, Alert, AlertHistory,
        Dashboard, DashboardWidget, MonitoringLog, MonitoringTrace, CapacityForecast,
    ):
        if model.__tablename__ not in existing:
            model.__table__.create(bind)


def downgrade():
    from infrastructure.database.models import (
        Alert, AlertHistory, CapacityForecast, Dashboard, DashboardWidget,
        Metric, MetricSample, MonitoringLog, MonitoringTrace, SystemHealth,
    )
    bind = op.get_bind()
    for model in (
        CapacityForecast, MonitoringTrace, MonitoringLog, DashboardWidget, Dashboard,
        AlertHistory, Alert, SystemHealth, MetricSample, Metric,
    ):
        model.__table__.drop(bind, checkfirst=True)
