"""Subjective daily check-ins.

Revision ID: 20260721_0017
Revises: 20260720_0016
Create Date: 2026-07-21

Adds ``subjective_check_ins``: a user's completed daily check-in feeding the
subjective recovery component (weight 0.05). Only a **completed** row
(``completed_at`` non-null) with all three normalized fields present is an
engine input; an absent or incomplete check-in omits the component, never a
neutral midpoint.

Values are stored already-normalized to [0, 1]. Versioned like facts: a partial
unique index keeps one current row per ``(tenant_id, local_health_day)``, and a
correction supersedes its predecessor rather than rewriting in place.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260721_0017"
down_revision: str | None = "20260720_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subjective_check_ins",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("local_health_day", sa.Text(), nullable=False),
        sa.Column("energy_n", sa.Float(), nullable=True),
        sa.Column("stress_n", sa.Float(), nullable=True),
        sa.Column("symptom_burden_n", sa.Float(), nullable=True),
        sa.Column("completed_at", sa.Text(), nullable=True),
        sa.Column("version_n", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("superseded_by", sa.Text(), nullable=True),
        sa.Column("superseded_at", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "energy_n IS NULL OR (energy_n >= 0 AND energy_n <= 1)",
            name="checkin_energy_range",
        ),
        sa.CheckConstraint(
            "stress_n IS NULL OR (stress_n >= 0 AND stress_n <= 1)",
            name="checkin_stress_range",
        ),
        sa.CheckConstraint(
            "symptom_burden_n IS NULL OR (symptom_burden_n >= 0 AND symptom_burden_n <= 1)",
            name="checkin_symptom_range",
        ),
        sa.CheckConstraint("version_n >= 1", name="checkin_version_n_pos"),
        sa.CheckConstraint("is_current IN (0, 1)", name="checkin_is_current_bool"),
        sa.CheckConstraint(
            "(superseded_by IS NULL AND superseded_at IS NULL) OR "
            "(superseded_by IS NOT NULL AND superseded_at IS NOT NULL AND is_current = 0)",
            name="checkin_supersede_pair",
        ),
        sa.CheckConstraint("length(local_health_day) = 10", name="checkin_local_day_format"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_subjective_check_ins_current",
        "subjective_check_ins",
        ["tenant_id", "local_health_day"],
        unique=True,
        sqlite_where=sa.text("is_current = 1"),
    )


def downgrade() -> None:
    op.drop_index("ux_subjective_check_ins_current", table_name="subjective_check_ins")
    op.drop_table("subjective_check_ins")
