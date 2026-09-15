"""M4.7 — Plugin SDK & Extension Framework

Additive only: four new tables (plugins, plugin_installations,
plugin_executions, plugin_hook_executions). No existing table, column,
index, or constraint is altered.

**Filename deviates from the brief's stated `0009_plugin_sdk.py`.**
This slice's real migration chain already has `0009_chaos_testing.py`
(M4.5) and `0010_security_hardening.py` (M4.6) — `0009` was not an
available number by the time this milestone was built. This migration
is numbered `0011` and chains onto `0010_security_hardening`, the
actual head of this slice's chain. See
`docs/M4.7_Plugin_SDK_Handover.md` §3 for this and the migration
chain's other standing caveats (same "confirm `down_revision` against
the real alembic history" note every M4.x migration in this slice has
carried since M4.4).

Revision ID: 0011_plugin_sdk
Revises: 0010_security_hardening
Create Date: 2026-07-18
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0011_plugin_sdk"
down_revision = "0010_security_hardening"  # NOTE: confirm/replace against the real head — see module docstring
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plugins",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("entrypoint", sa.String(), nullable=False),
        sa.Column("api_version", sa.String(), nullable=False, server_default="1.0.0"),
        sa.Column("permissions", sa.JSON(), nullable=True),
        sa.Column("dependencies", sa.JSON(), nullable=True),
        sa.Column("hooks", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "plugin_installations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("plugin_id", sa.String(), sa.ForeignKey("plugins.id"), nullable=False),
        sa.Column("state", sa.String(), nullable=False, server_default="installed"),
        sa.Column("source_type", sa.String(), nullable=False, server_default="python_package"),
        sa.Column("source_path", sa.String(), nullable=True),
        sa.Column("changed_at", sa.DateTime(), nullable=True),
        sa.Column("enabled_at", sa.DateTime(), nullable=True),
        sa.Column("disabled_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
    )
    op.create_index("ix_plugin_installations_plugin_id", "plugin_installations", ["plugin_id"])
    op.create_index("ix_plugin_installations_changed_at", "plugin_installations", ["changed_at"])

    op.create_table(
        "plugin_executions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("plugin_id", sa.String(), sa.ForeignKey("plugins.id"), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("executed_at", sa.DateTime(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
    )
    op.create_index("ix_plugin_executions_plugin_id", "plugin_executions", ["plugin_id"])
    op.create_index("ix_plugin_executions_action", "plugin_executions", ["action"])
    op.create_index("ix_plugin_executions_executed_at", "plugin_executions", ["executed_at"])

    op.create_table(
        "plugin_hook_executions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("plugin_id", sa.String(), sa.ForeignKey("plugins.id"), nullable=False),
        sa.Column("hook", sa.String(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("executed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_plugin_hook_executions_plugin_id", "plugin_hook_executions", ["plugin_id"])
    op.create_index("ix_plugin_hook_executions_hook", "plugin_hook_executions", ["hook"])
    op.create_index("ix_plugin_hook_executions_executed_at", "plugin_hook_executions", ["executed_at"])


def downgrade() -> None:
    op.drop_index("ix_plugin_hook_executions_executed_at", table_name="plugin_hook_executions")
    op.drop_index("ix_plugin_hook_executions_hook", table_name="plugin_hook_executions")
    op.drop_index("ix_plugin_hook_executions_plugin_id", table_name="plugin_hook_executions")
    op.drop_table("plugin_hook_executions")

    op.drop_index("ix_plugin_executions_executed_at", table_name="plugin_executions")
    op.drop_index("ix_plugin_executions_action", table_name="plugin_executions")
    op.drop_index("ix_plugin_executions_plugin_id", table_name="plugin_executions")
    op.drop_table("plugin_executions")

    op.drop_index("ix_plugin_installations_changed_at", table_name="plugin_installations")
    op.drop_index("ix_plugin_installations_plugin_id", table_name="plugin_installations")
    op.drop_table("plugin_installations")

    op.drop_table("plugins")
