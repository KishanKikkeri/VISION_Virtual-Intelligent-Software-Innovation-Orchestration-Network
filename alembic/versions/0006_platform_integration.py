"""M3.9 Platform Integration -- platform_reports, validation_results,
dependency_checks.
Revision: 0006
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    from sqlalchemy import inspect
    from infrastructure.database.models import DependencyCheck, PlatformReport, ValidationResult

    bind = op.get_bind()
    inspector = inspect(bind)
    existing = inspector.get_table_names()

    # Parent table first (validation_results/dependency_checks FK into platform_reports).
    for model in (PlatformReport, ValidationResult, DependencyCheck):
        if model.__tablename__ not in existing:
            model.__table__.create(bind)


def downgrade():
    from infrastructure.database.models import DependencyCheck, PlatformReport, ValidationResult
    bind = op.get_bind()
    for model in (DependencyCheck, ValidationResult, PlatformReport):
        model.__table__.drop(bind, checkfirst=True)
