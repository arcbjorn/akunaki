"""Users and backend-issued sessions.

Revision ID: 20260719_0011
Revises: 20260719_0010
Create Date: 2026-07-19

Adds ``users`` (identity from an OIDC issuer) and ``sessions`` (backend-issued
opaque cookie sessions).

Security rules enforced by the schema rather than by convention:

- **Never store the raw cookie token.** Only ``token_hash`` is persisted, and
  it is unique so a presented cookie is looked up by hash.
- **Never store a plaintext CSRF secret.** Only ``csrf_secret_hash``.
- ``expires_at`` must be after ``created_at``, and a revoked session records
  when it was revoked.

Scope note: the OIDC **handshake** is not built. The IdP itself is roadmap
open decision 1 (Auth0 / Cognito / Keycloak / …), so this revision adds the
identity and session *storage* those flows will write to, without pretending a
provider has been chosen. No ``oidc_states`` table is created here.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260719_0011"
down_revision: str | None = "20260719_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("oidc_issuer", sa.Text(), nullable=False),
        sa.Column("oidc_subject", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("length(oidc_issuer) > 0", name="user_issuer_nonempty"),
        sa.CheckConstraint("length(oidc_subject) > 0", name="user_subject_nonempty"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # Issuer alone is not unique; the pair is what identifies a person.
        sa.UniqueConstraint("oidc_issuer", "oidc_subject", name="uq_users_issuer_subject"),
    )
    op.create_index("ix_users_tenant", "users", ["tenant_id"], unique=False)

    op.create_table(
        "sessions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        # Hash only. The raw cookie token exists solely in the response to the
        # browser; a database dump must not yield usable session tokens.
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("csrf_secret_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("revoked_at", sa.Text(), nullable=True),
        sa.CheckConstraint("length(token_hash) > 0", name="session_token_hash_nonempty"),
        sa.CheckConstraint("length(csrf_secret_hash) > 0", name="session_csrf_hash_nonempty"),
        sa.CheckConstraint("expires_at > created_at", name="session_expiry_after_creation"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
    )
    op.create_index("ix_sessions_user", "sessions", ["user_id"], unique=False)
    # Supports the expiry sweep.
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_sessions_expires_at", table_name="sessions")
    op.drop_index("ix_sessions_user", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("ix_users_tenant", table_name="users")
    op.drop_table("users")
