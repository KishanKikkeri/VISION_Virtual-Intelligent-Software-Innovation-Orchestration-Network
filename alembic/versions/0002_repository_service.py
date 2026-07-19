"""M3.2 Repository Service — repositories, branches, pull_requests, repository_events.
Revision: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    from sqlalchemy import inspect
    from infrastructure.database.models import (
        Branch, PullRequest, Repository, RepositoryEvent,
    )

    bind = op.get_bind()
    inspector = inspect(bind)
    existing = inspector.get_table_names()

    for model in (Repository, Branch, PullRequest, RepositoryEvent):
        if model.__tablename__ not in existing:
            model.__table__.create(bind)


def downgrade():
    from infrastructure.database.models import (
        Branch, PullRequest, Repository, RepositoryEvent,
    )
    bind = op.get_bind()
    for model in (RepositoryEvent, PullRequest, Branch, Repository):
        model.__table__.drop(bind, checkfirst=True)
