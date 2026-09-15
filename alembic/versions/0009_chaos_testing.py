"""M4.5 — Chaos Testing & Resilience Framework

Additive only: four new tables (chaos_scenarios, chaos_runs,
fault_events, resilience_reports). No existing table, column, index,
or constraint is altered. See
infrastructure/database/chaos_testing_models.py for the ORM classes
this migration mirrors, and that module's docstring / M4.4's own
handover note for the caveat this migration shares: **`down_revision`
below must be updated** against the real alembic history — this
milestone's sandbox slice only had M4.4's own placeholder
`0008_benchmarking` migration to chain from, not the platform's real
0001-0007 (or a confirmed real 0008) history.

Revision ID: 0009_chaos_testing
Revises: 0008_benchmarking
Create Date: 2026-07-17
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0009_chaos_testing"
down_revision = "0008_benchmarking"  # NOTE: confirm/replace against the real head — see module docstring
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chaos_scenarios",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("fault_types", sa.JSON(), nullable=True),
        sa.Column("requires_external_infra", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_chaos_scenarios_name", "chaos_scenarios", ["name"], unique=True)

    op.create_table(
        "chaos_runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("scenario_id", sa.String(), sa.ForeignKey("chaos_scenarios.id"), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=True),
        sa.Column("workflow_version", sa.String(), nullable=True),
        sa.Column("platform_version", sa.String(), nullable=True),
        sa.Column("benchmark_version", sa.String(), nullable=True),
        sa.Column("commit_hash", sa.String(), nullable=True),
        sa.Column("environment", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("metrics", sa.JSON(), nullable=True),
        sa.Column("scenario_results", sa.JSON(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_chaos_runs_scenario_id", "chaos_runs", ["scenario_id"])
    op.create_index("ix_chaos_runs_timestamp", "chaos_runs", ["timestamp"])

    op.create_table(
        "fault_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("chaos_run_id", sa.String(), sa.ForeignKey("chaos_runs.id"), nullable=False),
        sa.Column("scenario_name", sa.String(), nullable=False),
        sa.Column("fault_type", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=False),
        sa.Column("injected_at", sa.DateTime(), nullable=True),
        sa.Column("triggered", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_fault_events_run_id", "fault_events", ["chaos_run_id"])
    op.create_index("ix_fault_events_scenario_name", "fault_events", ["scenario_name"])
    op.create_index("ix_fault_events_fault_type", "fault_events", ["fault_type"])
    op.create_index("ix_fault_events_target", "fault_events", ["target"])

    op.create_table(
        "resilience_reports",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("chaos_run_id", sa.String(), sa.ForeignKey("chaos_runs.id"), nullable=False, unique=True),
        sa.Column("resilience_score", sa.Float(), nullable=False),
        sa.Column("recommendations", sa.JSON(), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("generated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_resilience_reports_run_id", "resilience_reports", ["chaos_run_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_resilience_reports_run_id", table_name="resilience_reports")
    op.drop_table("resilience_reports")

    op.drop_index("ix_fault_events_target", table_name="fault_events")
    op.drop_index("ix_fault_events_fault_type", table_name="fault_events")
    op.drop_index("ix_fault_events_scenario_name", table_name="fault_events")
    op.drop_index("ix_fault_events_run_id", table_name="fault_events")
    op.drop_table("fault_events")

    op.drop_index("ix_chaos_runs_timestamp", table_name="chaos_runs")
    op.drop_index("ix_chaos_runs_scenario_id", table_name="chaos_runs")
    op.drop_table("chaos_runs")

    op.drop_index("ix_chaos_scenarios_name", table_name="chaos_scenarios")
    op.drop_table("chaos_scenarios")
