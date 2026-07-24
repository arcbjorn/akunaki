"""Workout sessions with canonical zone-load.

Revision ID: 20260722_0019
Revises: 20260722_0018
Create Date: 2026-07-22

Adds ``workout_sessions``, a typed one-to-one detail table for workouts whose
canonical training load is computed internally from HR-zone minutes (never a
vendor-provided load). Zone minutes are retained so the load can be recomputed
under a new zone-weight/formula version. The daily strain-load — the sum of a
day's included sessions — is what feeds the prior-load / ACWR recovery path.

Mirrors ``sleep_sessions``: one-to-one with a fact header via a PK/FK on
``fact_record_id`` (not EAV), tenant-scoped, cascade-deleted with its header.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260722_0019"
down_revision: str | None = "20260722_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workout_sessions",
        sa.Column("fact_record_id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("session_load", sa.Float(), nullable=False),
        sa.Column("zone1_min", sa.Float(), nullable=False),
        sa.Column("zone2_min", sa.Float(), nullable=False),
        sa.Column("zone3_min", sa.Float(), nullable=False),
        sa.Column("zone4_min", sa.Float(), nullable=False),
        sa.Column("zone5_min", sa.Float(), nullable=False),
        sa.CheckConstraint("session_load >= 0", name="workout_load_nonneg"),
        sa.CheckConstraint(
            "zone1_min >= 0 AND zone2_min >= 0 AND zone3_min >= 0 "
            "AND zone4_min >= 0 AND zone5_min >= 0",
            name="workout_zone_minutes_nonneg",
        ),
        sa.ForeignKeyConstraint(["fact_record_id"], ["fact_records.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("fact_record_id"),
    )


def downgrade() -> None:
    op.drop_table("workout_sessions")
