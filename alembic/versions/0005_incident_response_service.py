"""M3.8 Incident Response Service -- incidents, incident_timeline_events,
incident_evidence, recovery_actions, incident_reports.
Revision: 0005
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    from sqlalchemy import inspect
    from infrastructure.database.models import (
        Incident, IncidentEvidence, IncidentReportRecord,
        IncidentTimelineEvent, RecoveryAction,
    )

    bind = op.get_bind()
    inspector = inspect(bind)
    existing = inspector.get_table_names()

    # Parent table first (timeline_events/evidence/recovery_actions/reports FK into incidents).
    for model in (Incident, IncidentTimelineEvent, IncidentEvidence, RecoveryAction, IncidentReportRecord):
        if model.__tablename__ not in existing:
            model.__table__.create(bind)


def downgrade():
    from infrastructure.database.models import (
        Incident, IncidentEvidence, IncidentReportRecord,
        IncidentTimelineEvent, RecoveryAction,
    )
    bind = op.get_bind()
    for model in (IncidentReportRecord, RecoveryAction, IncidentEvidence, IncidentTimelineEvent, Incident):
        model.__table__.drop(bind, checkfirst=True)
