"""Source selections and candidates (daily-metric slice).

Revision ID: 20260724_0023
Revises: 20260723_0022
Create Date: 2026-07-24

Persists the deterministic per-day source-precedence decision as an auditable,
versioned record. This is the ``daily_metric`` slice of the design's source
selection: exactly one **current** decision per
``(tenant, metric_family, grain_key)`` (grain_key = the local health day), with
the losing providers recorded as ``source_selection_candidates`` for the "Why".

The session/workout grain machinery (``source_grains`` / ``source_grain_versions``
/ ``source_grain_members``) is **not** built here — daily-metric grains need no
membership snapshot, so ``source_grain_id`` / ``source_grain_version_id`` are
always null for this granularity. The granularity CHECK is limited to
``daily_metric`` and widens when session/workout selection ships.

``source_policy_version_id`` stores the policy version **string** (a code
constant, ``source_policy_v0.1.0``); the ``source_policies`` rules tables are not
built while the policy is code-defined rather than user-configurable.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260724_0023"
down_revision: str | None = "20260723_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_selections",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("metric_family", sa.Text(), nullable=False),
        sa.Column("granularity", sa.Text(), nullable=False),
        sa.Column("grain_key", sa.Text(), nullable=False),
        sa.Column("local_health_day", sa.Text(), nullable=True),
        sa.Column("selected_fact_record_id", sa.Text(), nullable=True),
        sa.Column("source_policy_version_id", sa.Text(), nullable=False),
        sa.Column("selection_reason", sa.Text(), nullable=False),
        sa.Column("missing_reason", sa.Text(), nullable=True),
        sa.Column("version_n", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Integer(), nullable=False),
        sa.Column("superseded_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        # Daily-metric slice only for now.
        sa.CheckConstraint("granularity = 'daily_metric'", name="source_selection_granularity"),
        sa.CheckConstraint("is_current IN (0, 1)", name="source_selection_is_current_bool"),
        sa.CheckConstraint(
            "selection_reason IN ('policy_match', 'only_source', "
            "'user_override', 'missing_authoritative')",
            name="source_selection_reason",
        ),
        # missing_authoritative <=> no selected fact + a required missing reason.
        sa.CheckConstraint(
            "(selection_reason = 'missing_authoritative' "
            "AND selected_fact_record_id IS NULL AND missing_reason IS NOT NULL) "
            "OR (selection_reason != 'missing_authoritative' "
            "AND selected_fact_record_id IS NOT NULL AND missing_reason IS NULL)",
            name="source_selection_missing_consistency",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["selected_fact_record_id"], ["fact_records.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # One current decision per grain key.
    op.create_index(
        "uq_source_selection_current",
        "source_selections",
        ["tenant_id", "metric_family", "granularity", "grain_key"],
        unique=True,
        sqlite_where=sa.text("is_current = 1"),
    )

    op.create_table(
        "source_selection_candidates",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("source_selection_id", sa.Text(), nullable=False),
        sa.Column("fact_record_id", sa.Text(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("eligibility", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "eligibility IN ('eligible', 'ineligible')", name="source_candidate_eligibility"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_selection_id"], ["source_selections.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["fact_record_id"], ["fact_records.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_selection_id", "fact_record_id", name="uq_source_candidate_fact"
        ),
    )


def downgrade() -> None:
    op.drop_table("source_selection_candidates")
    op.drop_index("uq_source_selection_current", table_name="source_selections")
    op.drop_table("source_selections")
