"""Daily activity detail table.

Revision ID: 20260723_0022
Revises: 20260723_0021
Create Date: 2026-07-23

Adds ``daily_activity``: a one-to-one detail on a ``fact_records`` header for a
day's activity totals — ``steps`` (INTEGER, per the design's step counts) and
``active_minutes`` (moderate+ minutes). At least one of the two must be present
(an empty-signal row would carry nothing), and both are non-negative. Keyed by
the header's ``fact_record_id`` like the other detail tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260723_0022"
down_revision: str | None = "20260723_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "daily_activity",
        sa.Column("fact_record_id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("steps", sa.Integer(), nullable=True),
        sa.Column("active_minutes", sa.Float(), nullable=True),
        sa.CheckConstraint(
            "steps IS NOT NULL OR active_minutes IS NOT NULL",
            name="activity_at_least_one_signal",
        ),
        sa.CheckConstraint("steps IS NULL OR steps >= 0", name="activity_steps_nonneg"),
        sa.CheckConstraint(
            "active_minutes IS NULL OR active_minutes >= 0",
            name="activity_active_minutes_nonneg",
        ),
        sa.ForeignKeyConstraint(["fact_record_id"], ["fact_records.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("fact_record_id"),
    )


def downgrade() -> None:
    op.drop_table("daily_activity")
