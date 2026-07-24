"""Add overnight respiratory rate to the vitals detail.

Revision ID: 20260720_0016
Revises: 20260720_0015
Create Date: 2026-07-20

Adds ``overnight_vitals.respiratory_rate_bpm`` (overnight respiration rate,
breaths/min), a nonnegativity CHECK, and widens the "at least one signal"
invariant to include it.

The respiratory component feeds recovery with the ``-max(z, 0)`` directed
mapping: an elevated rate lowers the score, a low rate is not rewarded. Weight
0.05. It is supplementary — HRV or RHR still clears the recovery gate.

The invariant CHECK cannot be altered in place on SQLite, so the table is
rebuilt via ``batch_alter_table`` (recreate mode); the downgrade rebuilds the
prior three-signal table explicitly.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260720_0016"
down_revision: str | None = "20260720_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table(
        "overnight_vitals",
        schema=None,
        recreate="always",
        table_args=(
            sa.CheckConstraint("hrv_ms IS NULL OR hrv_ms >= 0", name="overnight_vitals_hrv_nonneg"),
            sa.CheckConstraint(
                "resting_hr_bpm IS NULL OR resting_hr_bpm >= 0",
                name="overnight_vitals_rhr_nonneg",
            ),
            sa.CheckConstraint(
                "respiratory_rate_bpm IS NULL OR respiratory_rate_bpm >= 0",
                name="overnight_vitals_resp_nonneg",
            ),
            sa.CheckConstraint(
                "hrv_ms IS NOT NULL OR resting_hr_bpm IS NOT NULL "
                "OR temperature_deviation_c IS NOT NULL OR respiratory_rate_bpm IS NOT NULL",
                name="overnight_vitals_at_least_one",
            ),
        ),
    ) as batch:
        batch.add_column(sa.Column("respiratory_rate_bpm", sa.Float(), nullable=True))


def downgrade() -> None:
    # Rebuild the prior three-signal table explicitly: a batch drop_column
    # cannot reconcile the respiratory-referencing CHECK with the copy step.
    op.rename_table("overnight_vitals", "overnight_vitals_old")
    op.create_table(
        "overnight_vitals",
        sa.Column("fact_record_id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("hrv_ms", sa.Float(), nullable=True),
        sa.Column("resting_hr_bpm", sa.Float(), nullable=True),
        sa.Column("temperature_deviation_c", sa.Float(), nullable=True),
        sa.CheckConstraint("hrv_ms IS NULL OR hrv_ms >= 0", name="overnight_vitals_hrv_nonneg"),
        sa.CheckConstraint(
            "resting_hr_bpm IS NULL OR resting_hr_bpm >= 0",
            name="overnight_vitals_rhr_nonneg",
        ),
        sa.CheckConstraint(
            "hrv_ms IS NOT NULL OR resting_hr_bpm IS NOT NULL "
            "OR temperature_deviation_c IS NOT NULL",
            name="overnight_vitals_at_least_one",
        ),
        sa.ForeignKeyConstraint(["fact_record_id"], ["fact_records.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("fact_record_id"),
    )
    # Only rows that still carry one of the three surviving signals are kept.
    op.execute(
        "INSERT INTO overnight_vitals "
        "(fact_record_id, tenant_id, hrv_ms, resting_hr_bpm, temperature_deviation_c) "
        "SELECT fact_record_id, tenant_id, hrv_ms, resting_hr_bpm, temperature_deviation_c "
        "FROM overnight_vitals_old "
        "WHERE hrv_ms IS NOT NULL OR resting_hr_bpm IS NOT NULL "
        "OR temperature_deviation_c IS NOT NULL"
    )
    op.drop_table("overnight_vitals_old")
