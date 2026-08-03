"""One-time expiring confirmations for mutating tool invocations.

Revision ID: 20260803_0025
Revises: 20260724_0024
Create Date: 2026-08-03

A confirmation authorizes **one specific call**, not a tool in general. The row
stores the full binding the design requires — tenant, user, run, tool name,
canonical args hash, idempotency key — so execution can reauthorize against
exactly what the user approved and argument substitution fails.

``token_hash`` follows the session/provenance pattern: only the SHA-256 of the
issued token is stored, so a database dump yields no usable confirmation, and
lookup is by hash rather than by comparing secrets.

``run_id`` is nullable: it identifies an agent conversation run, and no agent
ships yet. A direct (non-agent) confirmation has none, and null is part of the
binding — a confirmation issued outside a run never authorizes one inside it.

A partial unique index keeps at most one **pending** confirmation per
``(tenant_id, idempotency_key)``, so a client retrying the confirm request does
not accumulate parallel live authorizations for the same call.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260803_0025"
down_revision: str | None = "20260724_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_confirmations",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=True),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("args_hash", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("consumed_at", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'consumed', 'cancelled')",
            name="tool_confirmation_status",
        ),
        # A consumed row must record when, and a pending one must not claim to.
        sa.CheckConstraint(
            "(status = 'consumed' AND consumed_at IS NOT NULL) "
            "OR (status != 'consumed' AND consumed_at IS NULL)",
            name="tool_confirmation_consumed_consistency",
        ),
        sa.CheckConstraint("length(token_hash) = 64", name="tool_confirmation_token_hash_len"),
        sa.CheckConstraint("length(args_hash) = 64", name="tool_confirmation_args_hash_len"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_tool_confirmation_token"),
    )
    # At most one live authorization per call, so a retried confirm request
    # cannot leave two usable confirmations behind.
    op.create_index(
        "ux_tool_confirmations_pending",
        "tool_confirmations",
        ["tenant_id", "idempotency_key"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_tool_confirmations_expiry",
        "tool_confirmations",
        ["status", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_tool_confirmations_expiry", table_name="tool_confirmations")
    op.drop_index("ux_tool_confirmations_pending", table_name="tool_confirmations")
    op.drop_table("tool_confirmations")
