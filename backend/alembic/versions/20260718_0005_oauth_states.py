"""OAuth CSRF/PKCE state: single-use, expiring authorize rows.

Revision ID: 20260718_0005
Revises: 20260718_0004
Create Date: 2026-07-18

Stores only a **hash** of the OAuth ``state`` (never the raw value) and the
**envelope-encrypted** PKCE ``code_verifier``.  The exact redirect URI issued
at authorize time is retained so the callback can be matched exactly.  Rows are
single-use via ``consumed_at`` and expire via ``expires_at``.  Does not rewrite
revisions 0001 through 0004.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260718_0005"
down_revision: str | None = "20260718_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_states",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("state_hash", sa.Text(), nullable=False),
        sa.Column("code_verifier_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("code_verifier_key_version", sa.Text(), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("consumed_at", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "provider IN ('oura', 'google_health', 'polar')",
            name="oauth_state_provider",
        ),
        sa.CheckConstraint(
            "length(state_hash) > 0",
            name="oauth_state_hash_nonempty",
        ),
        sa.CheckConstraint(
            "length(code_verifier_ciphertext) > 0",
            name="oauth_state_verifier_nonempty",
        ),
        sa.CheckConstraint(
            "length(code_verifier_key_version) > 0",
            name="oauth_state_key_version_nonempty",
        ),
        sa.CheckConstraint(
            "length(redirect_uri) > 0",
            name="oauth_state_redirect_uri_nonempty",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="oauth_state_expiry_after_creation",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        # Lookup key on callback; unique so one state can never map to two rows.
        sa.UniqueConstraint("state_hash", name="uq_oauth_states_state_hash"),
    )
    # Supports the expiry purge sweep.
    op.create_index(
        "ix_oauth_states_expires_at",
        "oauth_states",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_oauth_states_expires_at", table_name="oauth_states")
    op.drop_table("oauth_states")
