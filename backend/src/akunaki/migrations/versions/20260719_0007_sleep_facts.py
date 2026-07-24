"""Canonical fact headers and the sleep detail table.

Revision ID: 20260719_0007
Revises: 20260719_0006
Create Date: 2026-07-19

Adds ``fact_records`` (the one header row every normalized measurement gets)
and ``sleep_sessions`` (a typed one-to-one detail table).

Scoped deliberately to the **sleep slice** that phase one's vertical slice
needs. The full data model defines many more detail tables (heart rate, HRV,
activity, workouts, labs, …); those arrive with the normalizers that populate
them rather than as empty tables now.

Design points enforced here rather than in application code:

- Facts are **versioned, never updated in place**: ``version_n`` increments and
  the prior row is superseded, so history is auditable.
- ``is_current`` is 0/1 with a partial unique index, so a logical fact can have
  at most **one** current version.
- ``normalizer_version`` lives here, not on raw revisions.
- Detail rows are one-to-one with a header via a PK/FK on ``fact_record_id``,
  not EAV and not a table-name string pointer.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260719_0007"
down_revision: str | None = "20260719_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- fact_records ----------------------------------------------------------
    op.create_table(
        "fact_records",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("connection_id", sa.Text(), nullable=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("vendor_record_id", sa.Text(), nullable=True),
        sa.Column("origin", sa.Text(), nullable=True),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("utc_instant", sa.Text(), nullable=True),
        sa.Column("start_utc", sa.Text(), nullable=True),
        sa.Column("end_utc", sa.Text(), nullable=True),
        sa.Column("source_offset_minutes", sa.Integer(), nullable=True),
        sa.Column("iana_timezone", sa.Text(), nullable=True),
        sa.Column("local_health_day", sa.Text(), nullable=True),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column("quality", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("freshness_at", sa.Text(), nullable=True),
        sa.Column("raw_revision_id", sa.Text(), nullable=True),
        sa.Column("raw_payload_id", sa.Text(), nullable=True),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("normalizer_version", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.Column("fact_key", sa.Text(), nullable=False),
        sa.Column("version_n", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("superseded_by", sa.Text(), nullable=True),
        sa.Column("superseded_at", sa.Text(), nullable=True),
        sa.Column("deletion_state", sa.Text(), nullable=False),
        sa.Column("exclude_from_load", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "provider IN ('oura', 'google_health', 'polar', 'manual', 'derived')",
            name="fact_provider",
        ),
        sa.CheckConstraint(
            "method IN ('wearable', 'user_entered', 'lab', 'derived')",
            name="fact_method",
        ),
        sa.CheckConstraint(
            "quality IN ('high', 'medium', 'low', 'unknown')",
            name="fact_quality",
        ),
        sa.CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="fact_confidence_range"),
        sa.CheckConstraint("version_n >= 1", name="fact_version_n_pos"),
        sa.CheckConstraint("is_current IN (0, 1)", name="fact_is_current_bool"),
        sa.CheckConstraint("exclude_from_load IN (0, 1)", name="fact_exclude_from_load_bool"),
        sa.CheckConstraint(
            "deletion_state IN ('active', 'vendor_deleted', 'privacy_scrubbed')",
            name="fact_deletion_state",
        ),
        # A superseded row is by definition not current, and carries both
        # pointer and timestamp or neither.
        sa.CheckConstraint(
            "(superseded_by IS NULL AND superseded_at IS NULL) OR "
            "(superseded_by IS NOT NULL AND superseded_at IS NOT NULL AND is_current = 0)",
            name="fact_supersede_pair",
        ),
        sa.CheckConstraint(
            "local_health_day IS NULL OR length(local_health_day) = 10",
            name="fact_local_day_format",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["connection_id"], ["connections.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["raw_revision_id"], ["raw_revisions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["raw_payload_id"], ["raw_payload.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fact_key", "version_n", name="uq_fact_records_key_version"),
    )
    # At most one current version per logical fact.
    op.create_index(
        "ux_fact_records_current",
        "fact_records",
        ["fact_key"],
        unique=True,
        sqlite_where=sa.text("is_current = 1"),
    )
    op.create_index(
        "ix_fact_records_day_lookup",
        "fact_records",
        ["tenant_id", "entity_type", "local_health_day", "is_current"],
        unique=False,
    )
    op.create_index(
        "ix_fact_records_raw_revision",
        "fact_records",
        ["tenant_id", "raw_revision_id"],
        unique=False,
    )

    # -- sleep_sessions --------------------------------------------------------
    op.create_table(
        "sleep_sessions",
        sa.Column("fact_record_id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("is_nap", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("duration_min", sa.Float(), nullable=False),
        sa.Column("time_in_bed_min", sa.Float(), nullable=True),
        sa.Column("efficiency_pct", sa.Float(), nullable=True),
        sa.Column("light_min", sa.Float(), nullable=True),
        sa.Column("deep_min", sa.Float(), nullable=True),
        sa.Column("rem_min", sa.Float(), nullable=True),
        sa.Column("awake_min", sa.Float(), nullable=True),
        sa.CheckConstraint("is_nap IN (0, 1)", name="sleep_session_is_nap_bool"),
        sa.CheckConstraint("duration_min >= 0", name="sleep_session_duration_nonneg"),
        sa.CheckConstraint(
            "efficiency_pct IS NULL OR (efficiency_pct >= 0 AND efficiency_pct <= 100)",
            name="sleep_session_efficiency_range",
        ),
        sa.ForeignKeyConstraint(["fact_record_id"], ["fact_records.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("fact_record_id"),
    )


def downgrade() -> None:
    op.drop_table("sleep_sessions")
    op.drop_index("ix_fact_records_raw_revision", table_name="fact_records")
    op.drop_index("ix_fact_records_day_lookup", table_name="fact_records")
    op.drop_index("ux_fact_records_current", table_name="fact_records")
    op.drop_table("fact_records")
