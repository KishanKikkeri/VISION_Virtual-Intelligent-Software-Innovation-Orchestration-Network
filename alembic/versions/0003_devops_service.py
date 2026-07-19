"""M3.6 DevOps Service -- deployments, deployment_history, deployment_health,
release_metadata, rollback_records.
Revision: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    from sqlalchemy import inspect
    from infrastructure.database.models import (
        Deployment, DeploymentHealth, DeploymentHistory,
        ReleaseMetadata, RollbackRecord,
    )

    bind = op.get_bind()
    inspector = inspect(bind)
    existing = inspector.get_table_names()

    # Parent table first (deployment_history/health/rollback_records FK into it).
    for model in (Deployment, DeploymentHistory, DeploymentHealth, ReleaseMetadata, RollbackRecord):
        if model.__tablename__ not in existing:
            model.__table__.create(bind)


def downgrade():
    from infrastructure.database.models import (
        Deployment, DeploymentHealth, DeploymentHistory,
        ReleaseMetadata, RollbackRecord,
    )
    bind = op.get_bind()
    for model in (RollbackRecord, ReleaseMetadata, DeploymentHealth, DeploymentHistory, Deployment):
        model.__table__.drop(bind, checkfirst=True)
