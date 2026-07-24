"""OIDC login state: CSRF ``state``, PKCE verifier, and replay ``nonce``.

Revision ID: 20260719_0012
Revises: 20260719_0011
Create Date: 2026-07-19

A **separate** table from ``oauth_states`` rather than a widened one, for two
reasons that are not stylistic:

1. ``oauth_states.tenant_id`` is a required FK, but at login time no tenant is
   known yet — establishing *who* the user is is the whole point of the flow.
2. ``oauth_states.provider`` is constrained to data providers
   (``oura``/``google_health``/``polar``). A login flow is not one of those,
   and loosening that CHECK would weaken a real guard on the connector path.

OIDC additionally requires a ``nonce``, which the connector flow has no use
for: ``state`` defends the *redirect* against CSRF, while ``nonce`` binds the
returned ``id_token`` to this specific authorization request and is verified
as a claim inside the token.

Both ``state`` and ``nonce`` are stored **hashed**; the PKCE verifier is stored
**envelope-encrypted**, matching the connector flow.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260719_0012"
down_revision: str | None = "20260719_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "login_states",
        sa.Column("id", sa.Text(), nullable=False),
        # Hash only: a leaked database must not yield a usable state value.
        sa.Column("state_hash", sa.Text(), nullable=False),
        # Hash only. The raw nonce is compared against the id_token claim.
        sa.Column("nonce_hash", sa.Text(), nullable=False),
        sa.Column("code_verifier_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("code_verifier_key_version", sa.Text(), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("consumed_at", sa.Text(), nullable=True),
        sa.CheckConstraint("length(state_hash) > 0", name="login_state_hash_nonempty"),
        sa.CheckConstraint("length(nonce_hash) > 0", name="login_state_nonce_hash_nonempty"),
        sa.CheckConstraint(
            "length(code_verifier_ciphertext) > 0",
            name="login_state_verifier_nonempty",
        ),
        sa.CheckConstraint(
            "length(code_verifier_key_version) > 0",
            name="login_state_key_version_nonempty",
        ),
        sa.CheckConstraint("length(redirect_uri) > 0", name="login_state_redirect_nonempty"),
        sa.CheckConstraint("expires_at > created_at", name="login_state_expiry_after_creation"),
        sa.PrimaryKeyConstraint("id"),
        # Lookup key on callback; unique so one state can never map to two rows.
        sa.UniqueConstraint("state_hash", name="uq_login_states_state_hash"),
    )
    # Supports the expiry purge sweep.
    op.create_index(
        "ix_login_states_expires_at",
        "login_states",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_login_states_expires_at", table_name="login_states")
    op.drop_table("login_states")
