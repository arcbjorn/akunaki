"""Scope job idempotency uniqueness to unsettled jobs.

Revision ID: 20260724_0025
Revises: 20260724_0024
Create Date: 2026-07-24

The table-level ``UNIQUE(tenant_id, idempotency_key)`` spanned *every* status,
including terminal ones. Because no code path clears the key when a job
settles, one succeeded row reserved its key forever: the reconciliation sweep
enqueued once and was then permanently deduped against its own completed job,
and each connection was reconcilable exactly once.

Dedupe is meant to prevent a *concurrent* duplicate ("one in-flight reconcile
refetch per connection"), not to consume the key for all time. The constraint
is rebuilt as a partial unique index over the live statuses only — ``ready``
and ``leased``. A retry returns a job to ``ready``, so it stays covered; the
terminal statuses (``succeeded``, ``failed``, ``cancelled``, ``dead_letter``)
release the key for the next legitimate enqueue.

The table-level UNIQUE cannot be dropped in place on SQLite, so the table is
rebuilt via ``batch_alter_table``, as in revision ``20260719_0010``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260724_0025"
down_revision: str | None = "20260724_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Statuses in which a job still owns its idempotency key. A retry goes back to
# 'ready', so both live statuses must be covered.
_LIVE_STATUS_PREDICATE = "status IN ('ready', 'leased')"


def upgrade() -> None:
    with op.batch_alter_table("jobs", schema=None) as batch:
        batch.drop_constraint("uq_jobs_tenant_idempotency_key", type_="unique")
    # Partial: only unsettled jobs reserve the key. NULL keys never conflict.
    op.create_index(
        "ux_jobs_live_idempotency_key",
        "jobs",
        ["tenant_id", "idempotency_key"],
        unique=True,
        sqlite_where=sa.text(_LIVE_STATUS_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index("ux_jobs_live_idempotency_key", table_name="jobs")
    # Restoring the all-status UNIQUE requires the data to satisfy it. Rows
    # that were legitimately re-enqueued under the partial index would now
    # collide, so drop the key on settled duplicates, keeping the newest
    # settled row per (tenant, key) — the live row, if any, always wins.
    op.execute(
        sa.text(
            """
            UPDATE jobs SET idempotency_key = NULL
            WHERE idempotency_key IS NOT NULL
              AND status NOT IN ('ready', 'leased')
              AND EXISTS (
                  SELECT 1 FROM jobs AS other
                  WHERE other.tenant_id = jobs.tenant_id
                    AND other.idempotency_key = jobs.idempotency_key
                    AND other.id <> jobs.id
                    AND (
                        other.status IN ('ready', 'leased')
                        OR other.created_at > jobs.created_at
                        OR (other.created_at = jobs.created_at AND other.id > jobs.id)
                    )
              )
            """
        )
    )
    with op.batch_alter_table("jobs", schema=None) as batch:
        batch.create_unique_constraint(
            "uq_jobs_tenant_idempotency_key", ["tenant_id", "idempotency_key"]
        )
