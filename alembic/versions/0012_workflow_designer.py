"""M4.8 — Visual Workflow Designer

Additive only: four new tables (workflow_layouts, designer_sessions,
canvas_snapshots, node_templates). No existing table, column, index, or
constraint is altered.

**Filename deviates from the brief's stated `0010_workflow_designer.py`.**
This slice's real migration chain already has `0011_plugin_sdk.py`
(M4.7, itself deviating from its own brief's stated `0009` — see that
migration's docstring). This migration is numbered `0012` and chains
onto `0011_plugin_sdk`, the actual head of this slice's chain. See
`docs/M4.8_Workflow_Designer_Handover.md` §3 for this and the migration
chain's other standing caveats (same "confirm `down_revision` against
the real alembic history" note every M4.x migration in this slice has
carried since M4.4).

Revision ID: 0012_workflow_designer
Revises: 0011_plugin_sdk
Create Date: 2026-07-18
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0012_workflow_designer"
down_revision = "0011_plugin_sdk"  # NOTE: confirm/replace against the real head — see module docstring
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_layouts",
        sa.Column("workflow_name", sa.String(), primary_key=True),
        sa.Column("version", sa.String(), nullable=False, server_default="0.0.1"),
        sa.Column("layout_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_workflow_layouts_updated_at", "workflow_layouts", ["updated_at"])

    op.create_table(
        "designer_sessions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("workflow_name", sa.String(), sa.ForeignKey("workflow_layouts.workflow_name"), nullable=False),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("opened_at", sa.DateTime(), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(), nullable=True),
        sa.Column("dirty", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("replay_mode", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_designer_sessions_workflow_name", "designer_sessions", ["workflow_name"])
    op.create_index("ix_designer_sessions_last_activity_at", "designer_sessions", ["last_activity_at"])

    op.create_table(
        "canvas_snapshots",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("workflow_name", sa.String(), sa.ForeignKey("workflow_layouts.workflow_name"), nullable=False),
        sa.Column("layout_json", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False, server_default="save"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_canvas_snapshots_workflow_name", "canvas_snapshots", ["workflow_name"])
    op.create_index("ix_canvas_snapshots_created_at", "canvas_snapshots", ["created_at"])

    op.create_table(
        "node_templates",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("node_type", sa.String(), nullable=False, server_default="custom"),
        sa.Column("category", sa.String(), nullable=False, server_default="custom"),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("icon", sa.String(), nullable=True),
        sa.Column("default_config", sa.JSON(), nullable=True),
        sa.Column("property_schema", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(), nullable=False, server_default="custom"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("node_templates")

    op.drop_index("ix_canvas_snapshots_created_at", table_name="canvas_snapshots")
    op.drop_index("ix_canvas_snapshots_workflow_name", table_name="canvas_snapshots")
    op.drop_table("canvas_snapshots")

    op.drop_index("ix_designer_sessions_last_activity_at", table_name="designer_sessions")
    op.drop_index("ix_designer_sessions_workflow_name", table_name="designer_sessions")
    op.drop_table("designer_sessions")

    op.drop_index("ix_workflow_layouts_updated_at", table_name="workflow_layouts")
    op.drop_table("workflow_layouts")
