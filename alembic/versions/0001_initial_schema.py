"""Initial schema — all 38 tables.
Revision: 0001
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, ARRAY

revision  = "0001"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    # Full schema is applied via SQLAlchemy metadata in development.
    # In production, generate with: alembic revision --autogenerate -m "initial"
    from sqlalchemy import inspect
    from infrastructure.database.connection import Base
    # Check if tables already exist before creating
    bind = op.get_bind()
    inspector = inspect(bind)
    existing = inspector.get_table_names()
    if "users" not in existing:
        Base.metadata.create_all(bind)

def downgrade():
    from infrastructure.database.connection import Base
    Base.metadata.drop_all(op.get_bind())
