"""Privacy deletion pipeline state.

Revision ID: 20260719_0009
Revises: 20260719_0008
Create Date: 2026-07-19

Adds ``deletion_requests`` (the pipeline state machine) and
``deletion_completion_proofs`` (the minimal, non-identifying audit artifact).

Scoped to phase one's exit criterion — "privacy delete stub cancels jobs and
scrubs demo tenant". The **restoration-suppression ledger** is deliberately
**not** created here: it requires a dedicated deletion key with access
separation from the primary app credentials, which does not exist yet.
Creating an empty ledger table would imply a guarantee the system cannot make.

The completion proof carries **counts only** — no health values, no email or
display name — per the security design's "minimal completion proof" rule.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260719_0009"
down_revision: str | None = "20260719_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deletion_requests",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("requested_at", sa.Text(), nullable=False),
        sa.Column("jobs_cancelled_at", sa.Text(), nullable=True),
        sa.Column("rows_scrubbed_at", sa.Text(), nullable=True),
        sa.Column("backups_scheduled_at", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.Text(), nullable=True),
        sa.Column("failure_class", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('requested', 'jobs_cancelled', 'rows_scrubbed', "
            "'backups_scheduled', 'completed', 'failed')",
            name="deletion_request_status",
        ),
        # No FK to tenants: the request must outlive the tenant row it scrubs,
        # otherwise completing a deletion would erase its own audit trail.
        sa.PrimaryKeyConstraint("id"),
        # One in-flight request per tenant; a completed one may be superseded.
        sa.UniqueConstraint("tenant_id", "requested_at", name="uq_deletion_requests_tenant_time"),
    )
    op.create_index(
        "ix_deletion_requests_status",
        "deletion_requests",
        ["status", "requested_at"],
        unique=False,
    )

    op.create_table(
        "deletion_completion_proofs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("deletion_request_id", sa.Text(), nullable=False),
        sa.Column("completed_at", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        # Counts only. Deliberately no tenant_id, no identity, no values.
        sa.Column("scrub_counts_json", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('completed', 'partial')",
            name="deletion_proof_status",
        ),
        sa.CheckConstraint(
            "json_valid(scrub_counts_json)",
            name="deletion_proof_counts_json_valid",
        ),
        sa.ForeignKeyConstraint(
            ["deletion_request_id"],
            ["deletion_requests.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deletion_request_id", name="uq_deletion_proofs_request"),
    )


def downgrade() -> None:
    op.drop_table("deletion_completion_proofs")
    op.drop_index("ix_deletion_requests_status", table_name="deletion_requests")
    op.drop_table("deletion_requests")
