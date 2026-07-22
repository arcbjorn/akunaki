"""Tracked anomaly intervals.

Revision ID: 20260722_0018
Revises: 20260721_0017
Create Date: 2026-07-22

Adds ``anomalies``: one interval per feature, opened when a detector's condition
holds and cleared only after the clear condition holds for two consecutive
local days. ``ended_on`` is null while open; a partial unique index keeps at
most one **active** anomaly per ``(tenant_id, feature_code)`` so re-detection
continues the open interval rather than duplicating it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260722_0018"
down_revision: str | None = "20260721_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "anomalies",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("feature_code", sa.Text(), nullable=False),
        sa.Column("started_on", sa.Text(), nullable=False),
        sa.Column("ended_on", sa.Text(), nullable=True),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("z_like", sa.Float(), nullable=True),
        sa.Column("formula_version", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "consecutive_clear_days",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("severity IN ('moderate', 'high')", name="anomaly_severity"),
        sa.CheckConstraint("is_active IN (0, 1)", name="anomaly_is_active_bool"),
        sa.CheckConstraint(
            "(is_active = 1 AND ended_on IS NULL) OR (is_active = 0 AND ended_on IS NOT NULL)",
            name="anomaly_active_open_pair",
        ),
        sa.CheckConstraint("consecutive_clear_days >= 0", name="anomaly_clear_days_nonneg"),
        sa.CheckConstraint("length(started_on) = 10", name="anomaly_started_format"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_anomalies_active",
        "anomalies",
        ["tenant_id", "feature_code"],
        unique=True,
        sqlite_where=sa.text("is_active = 1"),
    )


def downgrade() -> None:
    op.drop_index("ux_anomalies_active", table_name="anomalies")
    op.drop_table("anomalies")
