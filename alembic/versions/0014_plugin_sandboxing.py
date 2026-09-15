"""M5.1 — Plugin Sandboxing & Secure Execution Framework

Additive only: four new tables (sandbox_executions, sandbox_policies,
sandbox_violations, sandbox_metrics). No existing table, column,
index, or constraint is altered — matching this milestone's own
"strictly additive" mandate and every prior M4.x migration's
convention in this slice.

Chains onto `0013_production_readiness`, the actual head of this
slice's migration chain as of M4.10/the Phase 1-2 audit passes. Same
standing caveat every migration in this slice has carried since M4.4:
confirm `down_revision` against the real Alembic history before this
ships.

Revision ID: 0014_plugin_sandboxing
Revises: 0013_production_readiness
Create Date: 2026-07-21
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0014_plugin_sandboxing"
down_revision = "0013_production_readiness"  # NOTE: confirm/replace against the real head — see module docstring
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sandbox_executions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("plugin_id", sa.String(), sa.ForeignKey("plugins.id"), nullable=False),
        sa.Column("hook", sa.String(), nullable=False),
        sa.Column("execution_mode", sa.String(), nullable=False, server_default="none"),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("timed_out", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("crashed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("duration_ms", sa.Float(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_sandbox_executions_plugin_id", "sandbox_executions", ["plugin_id"])
    op.create_index("ix_sandbox_executions_hook", "sandbox_executions", ["hook"])
    op.create_index("ix_sandbox_executions_created_at", "sandbox_executions", ["created_at"])

    op.create_table(
        "sandbox_policies",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("plugin_id", sa.String(), sa.ForeignKey("plugins.id"), nullable=False),
        sa.Column("execution_mode", sa.String(), nullable=False, server_default="none"),
        sa.Column("allowed_permissions", sa.JSON(), nullable=True),
        sa.Column("limits", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_sandbox_policies_plugin_id", "sandbox_policies", ["plugin_id"])
    op.create_index("ix_sandbox_policies_created_at", "sandbox_policies", ["created_at"])

    op.create_table(
        "sandbox_violations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("plugin_id", sa.String(), sa.ForeignKey("plugins.id"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("permission", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_sandbox_violations_plugin_id", "sandbox_violations", ["plugin_id"])
    op.create_index("ix_sandbox_violations_kind", "sandbox_violations", ["kind"])
    op.create_index("ix_sandbox_violations_created_at", "sandbox_violations", ["created_at"])

    op.create_table(
        "sandbox_metrics",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("plugin_id", sa.String(), sa.ForeignKey("plugins.id"), nullable=False),
        sa.Column("execution_count", sa.Float(), nullable=False, server_default="0"),
        sa.Column("success_count", sa.Float(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Float(), nullable=False, server_default="0"),
        sa.Column("timeout_count", sa.Float(), nullable=False, server_default="0"),
        sa.Column("crash_count", sa.Float(), nullable=False, server_default="0"),
        sa.Column("violation_count", sa.Float(), nullable=False, server_default="0"),
        sa.Column("total_duration_ms", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_sandbox_metrics_plugin_id", "sandbox_metrics", ["plugin_id"])
    op.create_index("ix_sandbox_metrics_created_at", "sandbox_metrics", ["created_at"])


def downgrade() -> None:
    op.drop_table("sandbox_metrics")
    op.drop_table("sandbox_violations")
    op.drop_table("sandbox_policies")
    op.drop_table("sandbox_executions")
