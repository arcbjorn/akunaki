"""Job leases and leader leases for durable CAS claim and fencing.

Revision ID: 20260713_0002
Revises: 20260713_0001
Create Date: 2026-07-13

Adds job_leases (one active lease per job) and leader_leases (named
coordination rows). Does not rewrite revision 0001.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260713_0002"
down_revision: str | None = "20260713_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_leases",
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("lease_owner", sa.Text(), nullable=False),
        sa.Column("leased_until", sa.Text(), nullable=False),
        sa.Column("fence_token", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("fence_token >= 0", name="job_lease_fence_token_nonneg"),
        sa.CheckConstraint("length(lease_owner) > 0", name="job_lease_owner_nonempty"),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("job_id"),
    )
    op.create_index(
        "ix_job_leases_leased_until",
        "job_leases",
        ["leased_until"],
        unique=False,
    )
    op.create_index(
        "ix_job_leases_lease_owner",
        "job_leases",
        ["lease_owner"],
        unique=False,
    )

    op.create_table(
        "leader_leases",
        sa.Column("lease_name", sa.Text(), nullable=False),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("leased_until", sa.Text(), nullable=True),
        sa.Column("fence_token", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("fence_token >= 0", name="leader_lease_fence_token_nonneg"),
        sa.CheckConstraint("length(lease_name) > 0", name="leader_lease_name_nonempty"),
        sa.CheckConstraint(
            "(lease_owner IS NULL AND leased_until IS NULL) OR "
            "(lease_owner IS NOT NULL AND leased_until IS NOT NULL)",
            name="leader_lease_owner_expiry_pair",
        ),
        sa.CheckConstraint(
            "lease_owner IS NULL OR length(lease_owner) > 0",
            name="leader_lease_owner_null_or_nonempty",
        ),
        sa.PrimaryKeyConstraint("lease_name"),
    )
    op.create_index(
        "ix_leader_leases_leased_until",
        "leader_leases",
        ["leased_until"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_leader_leases_leased_until", table_name="leader_leases")
    op.drop_table("leader_leases")
    op.drop_index("ix_job_leases_lease_owner", table_name="job_leases")
    op.drop_index("ix_job_leases_leased_until", table_name="job_leases")
    op.drop_table("job_leases")
