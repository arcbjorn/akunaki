"""Bearer service tokens for non-browser callers.

Revision ID: 20260815_0028
Revises: 20260804_0027
Create Date: 2026-08-15

The security design reserves ``Authorization: Bearer`` for a service caller —
an external agent or MCP adapter that cannot hold a browser session. This
table backs that: a tenant- and user-bound credential whose **hash only** is
stored (issue shows the raw token once), read-scoped so it can never authorize
a mutation, revocable, with optional expiry.

``token_hash`` is unique because validation is an exact-match index probe, the
same discipline as sessions. ``scope`` is checked to the known values so an
out-of-band row cannot invent a broader grant than the code understands.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260815_0028"
down_revision: str | None = "20260804_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_tokens",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=True),
        sa.Column("revoked_at", sa.Text(), nullable=True),
        sa.CheckConstraint("length(name) > 0", name="service_token_name_nonempty"),
        sa.CheckConstraint("scope IN ('read')", name="service_token_scope_known"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_service_tokens_token_hash", "service_tokens", ["token_hash"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_service_tokens_token_hash", table_name="service_tokens")
    op.drop_table("service_tokens")
