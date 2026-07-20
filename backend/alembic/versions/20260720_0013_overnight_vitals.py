"""Overnight vitals detail table (HRV, resting HR).

Revision ID: 20260720_0013
Revises: 20260719_0012
Create Date: 2026-07-20

Adds ``overnight_vitals``, a typed one-to-one detail table for the overnight
HRV (RMSSD ms) and resting heart rate (bpm) metrics measured across the
principal sleep and keyed to the wake-date. These feed the two highest-weight
recovery components (HRV 0.25, RHR 0.15), so their arrival is what lets the
recovery gate pass for a real tenant.

Mirrors the ``sleep_sessions`` shape: one-to-one with a fact header via a
PK/FK on ``fact_record_id`` (not EAV), tenant-scoped, cascade-deleted with its
header. A row must carry at least one of the two metrics — a row with neither
holds no signal.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260720_0013"
down_revision: str | None = "20260719_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "overnight_vitals",
        sa.Column("fact_record_id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("hrv_ms", sa.Float(), nullable=True),
        sa.Column("resting_hr_bpm", sa.Float(), nullable=True),
        sa.CheckConstraint("hrv_ms IS NULL OR hrv_ms >= 0", name="overnight_vitals_hrv_nonneg"),
        sa.CheckConstraint(
            "resting_hr_bpm IS NULL OR resting_hr_bpm >= 0",
            name="overnight_vitals_rhr_nonneg",
        ),
        sa.CheckConstraint(
            "hrv_ms IS NOT NULL OR resting_hr_bpm IS NOT NULL",
            name="overnight_vitals_at_least_one",
        ),
        sa.ForeignKeyConstraint(["fact_record_id"], ["fact_records.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("fact_record_id"),
    )


def downgrade() -> None:
    op.drop_table("overnight_vitals")
