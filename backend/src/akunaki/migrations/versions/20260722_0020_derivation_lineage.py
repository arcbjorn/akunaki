"""Derivation lineage and opaque provenance.

Revision ID: 20260722_0020
Revises: 20260722_0019
Create Date: 2026-07-22

Adds ``derivation_runs`` (one reproducible derivation of an artifact, with an
opaque ``provenance_token_hash`` a day-response URL references) and
``derivation_inputs`` (typed input FKs, no polymorphic pointer). Also adds
``daily_health_scores.derivation_run_id`` so a served score can be traced.

Only the ``fact_record_id`` typed input FK is live today (features, baselines,
and source selections are not persisted yet), so the "exactly one typed FK"
CHECK reduces to requiring the fact FK; it widens with each new typed column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260722_0020"
down_revision: str | None = "20260722_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "derivation_runs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("artifact_kind", sa.Text(), nullable=False),
        sa.Column("local_health_day", sa.Text(), nullable=True),
        sa.Column("formula_version", sa.Text(), nullable=False),
        sa.Column("dependency_hash", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("freshness_at", sa.Text(), nullable=True),
        sa.Column("as_of_at", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("provenance_token", sa.Text(), nullable=False),
        sa.Column("superseded_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "artifact_kind IN ('feature', 'baseline', 'score', 'factor', "
            "'anomaly', 'recommendation')",
            name="derivation_artifact_kind",
        ),
        sa.CheckConstraint(
            "status IN ('ok', 'partial', 'insufficient')",
            name="derivation_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provenance_token", name="uq_derivation_token"),
    )

    op.create_table(
        "derivation_inputs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("derivation_run_id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("fact_record_id", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "fact_record_id IS NOT NULL", name="derivation_input_one_typed_fk"
        ),
        sa.ForeignKeyConstraint(
            ["derivation_run_id"], ["derivation_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["fact_record_id"], ["fact_records.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # A nullable FK column: a score written before its run has none.
    with op.batch_alter_table("daily_health_scores", schema=None) as batch:
        batch.add_column(sa.Column("derivation_run_id", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("daily_health_scores", schema=None) as batch:
        batch.drop_column("derivation_run_id")
    op.drop_table("derivation_inputs")
    op.drop_table("derivation_runs")
