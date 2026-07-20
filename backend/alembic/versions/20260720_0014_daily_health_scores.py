"""Daily health scores and their signed factors.

Revision ID: 20260720_0014
Revises: 20260720_0013
Create Date: 2026-07-20

Adds ``daily_health_scores`` (one computed score per ``score_code`` per local
health day) and ``score_factors`` (the signed contributors, for disclosure).

Scores are **versioned, never rewritten in place**: a partial unique index
keeps at most one current row per ``(tenant_id, local_health_day, score_code)``,
and a changed value supersedes its predecessor. Formula/policy changes are
recorded as new ``formula_version`` rows, never in-place edits, so score
history stays auditable.

Only score codes with an accepted formula may be written; ``recovery`` under
``general_recovery_v0.1.0`` is the only shippable score in v0.1.0. The registry
CHECK reserves the others so a mis-supplied code is rejected at the boundary.

``derivation_run_id`` from the data model is intentionally omitted here: the
derivation-run subsystem is separate, deferred work, and a nullable FK to a
table that does not exist would encode a guarantee the system cannot make.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260720_0014"
down_revision: str | None = "20260720_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "daily_health_scores",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("local_health_day", sa.Text(), nullable=False),
        sa.Column("score_code", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("available_weight", sa.Float(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("formula_version", sa.Text(), nullable=False),
        sa.Column("dependency_hash", sa.Text(), nullable=False),
        sa.Column("freshness_at", sa.Text(), nullable=True),
        sa.Column("as_of_at", sa.Text(), nullable=True),
        sa.Column("version_n", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("superseded_by", sa.Text(), nullable=True),
        sa.Column("superseded_at", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "score_code IN ('recovery', 'sleep', 'strain', 'activity', 'readiness')",
            name="score_code_registry",
        ),
        sa.CheckConstraint(
            "status IN ('ok', 'partial', 'insufficient')",
            name="score_status",
        ),
        sa.CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 100)",
            name="score_range",
        ),
        sa.CheckConstraint(
            "(status = 'insufficient') = (score IS NULL)",
            name="score_null_iff_insufficient",
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0", name="score_confidence_range"
        ),
        sa.CheckConstraint("version_n >= 1", name="score_version_n_pos"),
        sa.CheckConstraint("is_current IN (0, 1)", name="score_is_current_bool"),
        sa.CheckConstraint(
            "(superseded_by IS NULL AND superseded_at IS NULL) OR "
            "(superseded_by IS NOT NULL AND superseded_at IS NOT NULL AND is_current = 0)",
            name="score_supersede_pair",
        ),
        sa.CheckConstraint("length(local_health_day) = 10", name="score_local_day_format"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_daily_health_scores_current",
        "daily_health_scores",
        ["tenant_id", "local_health_day", "score_code"],
        unique=True,
        sqlite_where=sa.text("is_current = 1"),
    )

    op.create_table(
        "score_factors",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("daily_health_score_id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("factor_code", sa.Text(), nullable=False),
        sa.Column("sign", sa.Integer(), nullable=False),
        sa.Column("magnitude", sa.Float(), nullable=False),
        sa.Column("weight", sa.Float(), nullable=True),
        sa.Column("present", sa.Integer(), nullable=False),
        sa.CheckConstraint("sign IN (-1, 0, 1)", name="score_factor_sign"),
        sa.CheckConstraint("present IN (0, 1)", name="score_factor_present_bool"),
        sa.ForeignKeyConstraint(
            ["daily_health_score_id"], ["daily_health_scores.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("score_factors")
    op.drop_index("ux_daily_health_scores_current", table_name="daily_health_scores")
    op.drop_table("daily_health_scores")
