"""M4.9 — Production Readiness & Release Engineering

Additive only: five new tables (releases, deployment_profiles,
backup_records, restore_records, environment_checks). No existing
table, column, index, or constraint is altered.

Chains onto `0012_workflow_designer`, the actual head of this slice's
migration chain (see that migration's own docstring for the "confirm
`down_revision` against the real alembic history" standing caveat this
slice's chain has carried since M4.4 — this migration inherits the same
caveat: before this ships, confirm this really is head against the
platform's real Alembic history).

Revision ID: 0013_production_readiness
Revises: 0012_workflow_designer
Create Date: 2026-07-19
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0013_production_readiness"
down_revision = "0012_workflow_designer"  # NOTE: confirm/replace against the real head — see module docstring
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "releases",
        sa.Column("version", sa.String(), primary_key=True),
        sa.Column("previous_version", sa.String(), nullable=True),
        sa.Column("channel", sa.String(), nullable=False, server_default="stable"),
        sa.Column("release_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_releases_created_at", "releases", ["created_at"])

    op.create_table(
        "deployment_profiles",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("environment", sa.String(), nullable=False),
        sa.Column("profile_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_deployment_profiles_environment", "deployment_profiles", ["environment"])
    op.create_index("ix_deployment_profiles_created_at", "deployment_profiles", ["created_at"])

    op.create_table(
        "backup_records",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("backup_type", sa.String(), nullable=False, server_default="full"),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("location", sa.String(), nullable=False),
        sa.Column("checksum", sa.String(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(), nullable=False, server_default="completed"),
        sa.Column("baseline_backup_id", sa.String(), nullable=True),
        sa.Column("notes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_backup_records_created_at", "backup_records", ["created_at"])

    op.create_table(
        "restore_records",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("backup_id", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False, server_default="dry_run"),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="planned"),
        sa.Column("confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("validation_issues", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_restore_records_backup_id", "restore_records", ["backup_id"])
    op.create_index("ix_restore_records_created_at", "restore_records", ["created_at"])

    op.create_table(
        "environment_checks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("environment", sa.String(), nullable=False),
        sa.Column("overall_status", sa.String(), nullable=False, server_default="skipped"),
        sa.Column("report_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_environment_checks_environment", "environment_checks", ["environment"])
    op.create_index("ix_environment_checks_created_at", "environment_checks", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_environment_checks_created_at", table_name="environment_checks")
    op.drop_index("ix_environment_checks_environment", table_name="environment_checks")
    op.drop_table("environment_checks")

    op.drop_index("ix_restore_records_created_at", table_name="restore_records")
    op.drop_index("ix_restore_records_backup_id", table_name="restore_records")
    op.drop_table("restore_records")

    op.drop_index("ix_backup_records_created_at", table_name="backup_records")
    op.drop_table("backup_records")

    op.drop_index("ix_deployment_profiles_created_at", table_name="deployment_profiles")
    op.drop_index("ix_deployment_profiles_environment", table_name="deployment_profiles")
    op.drop_table("deployment_profiles")

    op.drop_index("ix_releases_created_at", table_name="releases")
    op.drop_table("releases")
