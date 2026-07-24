"""Durable job execution schema: job_type, attempts, dead letters.

Revision ID: 20260713_0003
Revises: 20260713_0002
Create Date: 2026-07-13

Adds job_type to jobs (non-null TEXT, system.noop default for existing
rows), last_error_class nullable TEXT, an index supporting role+job_type+
status+run_after, the job_attempts table for per-attempt tracking, and
the job_dead_letters table for permanent failure records.  Does not
rewrite revision 0001 or 0002.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260713_0003"
down_revision: str | None = "20260713_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- jobs table additions --------------------------------------------------
    op.add_column(
        "jobs",
        sa.Column(
            "job_type",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'system.noop'"),
        ),
    )
    op.add_column(
        "jobs",
        sa.Column("last_error_class", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_jobs_role_job_type_status_run_after",
        "jobs",
        ["role", "job_type", "status", "run_after"],
        unique=False,
    )

    # -- job_attempts table ----------------------------------------------------
    op.create_table(
        "job_attempts",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("fence_token", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_class", sa.Text(), nullable=True),
        sa.Column("redacted_error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("finished_at", sa.Text(), nullable=True),
        sa.CheckConstraint("attempt_number >= 1", name="job_attempt_number_pos"),
        sa.CheckConstraint("fence_token >= 0", name="job_attempt_fence_token_nonneg"),
        sa.CheckConstraint("length(lease_owner) > 0", name="job_attempt_lease_owner_nonempty"),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'retry_scheduled', 'dead_letter', 'lease_expired')",
            name="job_attempt_status",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id",
            "attempt_number",
            name="uq_job_attempts_job_id_attempt_number",
        ),
    )
    op.create_index(
        "ix_job_attempts_job_id",
        "job_attempts",
        ["job_id"],
        unique=False,
    )
    op.create_index(
        "ix_job_attempts_status",
        "job_attempts",
        ["status"],
        unique=False,
    )

    # -- job_dead_letters table ------------------------------------------------
    op.create_table(
        "job_dead_letters",
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("fence_token", sa.Integer(), nullable=False),
        sa.Column("error_class", sa.Text(), nullable=False),
        sa.Column("redacted_error_message", sa.Text(), nullable=True),
        sa.Column("dead_lettered_at", sa.Text(), nullable=False),
        sa.CheckConstraint("attempt_number >= 1", name="job_dl_attempt_number_pos"),
        sa.CheckConstraint("fence_token >= 0", name="job_dl_fence_token_nonneg"),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("job_id"),
    )
    op.create_index(
        "ix_job_dead_letters_tenant_dead_lettered_at",
        "job_dead_letters",
        ["tenant_id", "dead_lettered_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_job_dead_letters_tenant_dead_lettered_at", table_name="job_dead_letters")
    op.drop_table("job_dead_letters")

    op.drop_index("ix_job_attempts_status", table_name="job_attempts")
    op.drop_index("ix_job_attempts_job_id", table_name="job_attempts")
    op.drop_table("job_attempts")

    op.drop_index("ix_jobs_role_job_type_status_run_after", table_name="jobs")
    op.drop_column("jobs", "last_error_class")
    op.drop_column("jobs", "job_type")
