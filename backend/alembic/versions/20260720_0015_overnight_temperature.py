"""Add overnight temperature deviation to the vitals detail.

Revision ID: 20260720_0015
Revises: 20260720_0014
Create Date: 2026-07-20

Adds ``overnight_vitals.temperature_deviation_c`` (overnight temperature
deviation vs baseline, °C) and widens the "at least one signal" invariant to
include it, so a night with only a temperature reading is a valid row.

The temperature component feeds recovery with the ``-|z|`` directed mapping:
any departure from baseline, in either direction, lowers the score. It is
supplementary — HRV or RHR is still what clears the recovery gate.

The invariant CHECK cannot be altered in place on SQLite, so the table is
rebuilt via ``batch_alter_table`` (recreate mode).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260720_0015"
down_revision: str | None = "20260720_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Force a table rebuild (recreate) so the widened invariant CHECK is applied;
    # a plain ADD COLUMN would leave the old two-signal CHECK in place.
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
                "hrv_ms IS NOT NULL OR resting_hr_bpm IS NOT NULL "
                "OR temperature_deviation_c IS NOT NULL",
                name="overnight_vitals_at_least_one",
            ),
        ),
    ) as batch:
        batch.add_column(sa.Column("temperature_deviation_c", sa.Float(), nullable=True))


def downgrade() -> None:
    # Rebuild the original two-signal table explicitly. A batch drop_column
    # cannot reconcile the temperature-referencing CHECK with the copy step, so
    # recreate the table from scratch and copy the rows that still fit.
    op.rename_table("overnight_vitals", "overnight_vitals_old")
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
    # Only rows with an HRV or RHR signal survive the narrowed invariant.
    op.execute(
        "INSERT INTO overnight_vitals (fact_record_id, tenant_id, hrv_ms, resting_hr_bpm) "
        "SELECT fact_record_id, tenant_id, hrv_ms, resting_hr_bpm FROM overnight_vitals_old "
        "WHERE hrv_ms IS NOT NULL OR resting_hr_bpm IS NOT NULL"
    )
    op.drop_table("overnight_vitals_old")
