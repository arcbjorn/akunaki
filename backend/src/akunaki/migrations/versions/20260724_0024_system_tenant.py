"""Seed the reserved system tenant.

Revision ID: 20260724_0024
Revises: 20260724_0023
Create Date: 2026-07-24

Inserts one reserved ``system`` tenant that owns platform-scoped rows — chiefly
system-wide periodic jobs (the reconciliation sweep), whose ``jobs.tenant_id``
FK must reference a real tenant even though the work belongs to no single
customer. Seeding it in a migration guarantees every deployment has exactly one.

Nothing user-facing is attributed to this tenant. The insert is idempotent
(``INSERT OR IGNORE``) so re-running the upgrade is safe; downgrade removes it.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260724_0024"
down_revision: str | None = "20260724_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SYSTEM_TENANT_ID = "system"


def upgrade() -> None:
    op.execute(
        "INSERT OR IGNORE INTO tenants "
        "(id, created_at, status, primary_timezone, display_name) "
        f"VALUES ('{_SYSTEM_TENANT_ID}', '1970-01-01T00:00:00Z', 'active', 'UTC', 'System')"
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM tenants WHERE id = '{_SYSTEM_TENANT_ID}'")
