"""Connection lifecycle schema: connections, secrets, health.

Revision ID: 20260718_0004
Revises: 20260713_0003
Create Date: 2026-07-18

Adds the per-tenant provider connection row, its envelope-encrypted secret
material (ciphertext BLOB only; tokens are never plaintext columns), and its
operational health counters.  OAuth state and the raw/sync transport tables
are deliberately deferred to the OAuth flow revision.  Does not rewrite
revisions 0001 through 0003.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260718_0004"
down_revision: str | None = "20260713_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- connections -----------------------------------------------------------
    op.create_table(
        "connections",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("scopes_granted_json", sa.Text(), nullable=False),
        sa.Column("external_user_id", sa.Text(), nullable=True),
        sa.Column("connected_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "provider IN ('oura', 'google_health', 'polar')",
            name="connection_provider",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'needs_reauth', 'revoked', 'error')",
            name="connection_status",
        ),
        sa.CheckConstraint(
            "json_valid(scopes_granted_json)",
            name="connection_scopes_json_valid",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            name="uq_connections_tenant_provider",
        ),
    )
    op.create_index(
        "ix_connections_tenant_status",
        "connections",
        ["tenant_id", "status"],
        unique=False,
    )

    # -- connection_secrets ----------------------------------------------------
    # Envelope-encrypted token material only. No plaintext token columns exist
    # on any table by design.
    op.create_table(
        "connection_secrets",
        sa.Column("connection_id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.Text(), nullable=False),
        sa.Column("rotated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("length(ciphertext) > 0", name="connection_secret_ciphertext_nonempty"),
        sa.CheckConstraint("length(key_version) > 0", name="connection_secret_key_version_nonempty"),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("connection_id"),
    )

    # -- connection_health -----------------------------------------------------
    op.create_table(
        "connection_health",
        sa.Column("connection_id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("last_success_at", sa.Text(), nullable=True),
        sa.Column("last_error_class", sa.Text(), nullable=True),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("rate_limit_reset_at", sa.Text(), nullable=True),
        sa.Column("webhook_last_verified_at", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name="connection_health_failures_nonneg",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("connection_id"),
    )


def downgrade() -> None:
    op.drop_table("connection_health")
    op.drop_table("connection_secrets")
    op.drop_index("ix_connections_tenant_status", table_name="connections")
    op.drop_table("connections")
