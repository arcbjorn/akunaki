"""Minimal platform foundation: tenants and jobs.

Revision ID: 20260713_0001
Revises:
Create Date: 2026-07-13

Full product schema and job concurrency protocol remain pending.
IDs are caller-supplied TEXT (no UUIDv7 generator in this revision).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260713_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "primary_timezone",
            sa.Text(),
            server_default=sa.text("'UTC'"),
            nullable=False,
        ),
        sa.Column("display_name", sa.Text(), nullable=True),
        # Short constraint_name tokens; MetaData naming convention expands them.
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'pending_delete')",
            name="tenant_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), server_default=sa.text("100"), nullable=False),
        sa.Column("run_after", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("fence_token", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("role IN ('core', 'agent')", name="job_role"),
        sa.CheckConstraint(
            "status IN ('ready', 'leased', 'succeeded', 'failed', 'cancelled', 'dead_letter')",
            name="job_status",
        ),
        sa.CheckConstraint("json_valid(payload_json)", name="job_payload_json_valid"),
        sa.CheckConstraint("attempts >= 0", name="job_attempts_nonneg"),
        sa.CheckConstraint("max_attempts >= 1", name="job_max_attempts_pos"),
        sa.CheckConstraint("fence_token >= 0", name="job_fence_token_nonneg"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_jobs_tenant_idempotency_key",
        ),
    )
    op.create_index(
        "ix_jobs_due",
        "jobs",
        ["status", "run_after", "priority", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_jobs_tenant_status",
        "jobs",
        ["tenant_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_jobs_role_status_run_after",
        "jobs",
        ["role", "status", "run_after"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_role_status_run_after", table_name="jobs")
    op.drop_index("ix_jobs_tenant_status", table_name="jobs")
    op.drop_index("ix_jobs_due", table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("tenants")
