"""Admit the ``read_sync`` service-token scope.

Revision ID: 20260815_0029
Revises: 20260815_0028
Create Date: 2026-08-15

``service_tokens.scope`` was checked to a single value, ``read``, because a
service token could not authorize any mutation at all. It now may authorize
one narrow class of them: a tool whose declared ``ConfirmationPolicy`` is
``IF_AGENT`` — today only ``connections.sync``, an idempotent, deduplicated
enqueue of the same job a webhook already queues. An ``ALWAYS`` tool
(``privacy.delete``) stays refused for every service token regardless of scope.

The CHECK is widened rather than dropped: the point of constraining the column
is that an out-of-band row cannot invent a grant the code does not understand,
and that property is worth keeping now that there is more than one value to
confuse. Existing rows are untouched and keep ``read``, so no live token gains
authority from this migration — the capability is opt-in at mint time only.

SQLite cannot alter a CHECK in place, and ``batch_alter_table`` is not usable
here: it reflects the live table, so the old constraint is carried into the
rebuilt one *alongside* the replacement (the reflected name carries the
``ck_`` convention prefix, so the two do not collide and both survive — leaving
a table that still rejects ``read_sync``). The table is therefore rebuilt
explicitly, which is also the only form in which the constraint set is visible
in the migration itself.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260815_0029"
down_revision: str | None = "20260815_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    "id",
    "tenant_id",
    "user_id",
    "name",
    "scope",
    "token_hash",
    "created_at",
    "expires_at",
    "revoked_at",
)


def _rebuild(*, scope_check: str) -> None:
    """Recreate ``service_tokens`` with ``scope_check``, preserving every row.

    Shared by both directions: the table definition differs only in the CHECK,
    so writing it twice would invite the two copies to drift.
    """
    op.rename_table("service_tokens", "service_tokens_old")
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
        sa.CheckConstraint(scope_check, name="service_token_scope_known"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    columns = ", ".join(_COLUMNS)
    op.execute(f"INSERT INTO service_tokens ({columns}) SELECT {columns} FROM service_tokens_old")
    op.drop_table("service_tokens_old")
    # The index is dropped with the old table, so it is recreated rather than
    # renamed: validation is an exact-match probe on the hash, and losing the
    # unique index would make that a table scan and admit a duplicate hash.
    op.create_index("ix_service_tokens_token_hash", "service_tokens", ["token_hash"], unique=True)


def upgrade() -> None:
    _rebuild(scope_check="scope IN ('read', 'read_sync')")


def downgrade() -> None:
    # Narrowing the CHECK would orphan any token minted with the new scope, so
    # revoke those rows before the copy rather than leave a live credential the
    # restored constraint says cannot exist. Revocation, not deletion: the
    # operator's `list` output keeps showing that the token was issued.
    op.execute(
        "UPDATE service_tokens SET revoked_at = created_at "
        "WHERE scope = 'read_sync' AND revoked_at IS NULL"
    )
    op.execute("UPDATE service_tokens SET scope = 'read' WHERE scope = 'read_sync'")
    _rebuild(scope_check="scope IN ('read')")
