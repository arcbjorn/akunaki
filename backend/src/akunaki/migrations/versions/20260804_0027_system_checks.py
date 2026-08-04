"""Durable results of scheduled system checks.

Revision ID: 20260804_0027
Revises: 20260804_0026
Create Date: 2026-08-04

A scheduled check runs on the **worker**; its result needs reading from the
**API**. Process-local metrics cannot bridge that — each process serves its own
registry — so a worker-computed verdict published only as a gauge reaches
nobody. This table is the bridge: the worker writes the verdict, `/readyz`
reads it.

One current row per ``name``, overwritten in place. This is deliberately **not**
versioned like facts or scores: it is a latest-known-state cell, not a history,
and a check that ran a thousand times should not leave a thousand rows to scan
on a readiness probe. The audit trail is where durable history lives.

``ok`` plus ``detail`` rather than a free-form status: a probe needs a boolean
it can gate or alarm on, and a bounded, PHI-free detail string for the human who
follows up.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260804_0027"
down_revision: str | None = "20260804_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "system_checks",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("ok", sa.Integer(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("checked_at", sa.Text(), nullable=False),
        sa.CheckConstraint("ok IN (0, 1)", name="system_check_ok_bool"),
        sa.CheckConstraint("length(name) > 0", name="system_check_name_nonempty"),
        # Bounded so a check cannot turn this into a log sink.
        sa.CheckConstraint(
            "detail IS NULL OR length(detail) <= 200", name="system_check_detail_len"
        ),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade() -> None:
    op.drop_table("system_checks")
