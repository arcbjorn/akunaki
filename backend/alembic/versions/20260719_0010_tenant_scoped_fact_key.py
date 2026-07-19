"""Scope the fact one-current index by tenant.

Revision ID: 20260719_0010
Revises: 20260719_0009
Create Date: 2026-07-19

``fact_key`` is derived from a vendor record id, which is only unique *within*
a tenant. The original indexes keyed on ``fact_key`` alone, so two tenants
whose providers issued the same record id collided: the second tenant's fact
was treated as a new version of the first tenant's, and its data was never
stored.

Both the partial one-current index and the (key, version) uniqueness are
re-created with ``tenant_id`` leading.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260719_0010"
down_revision: str | None = "20260719_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ux_fact_records_current", table_name="fact_records")
    op.create_index(
        "ux_fact_records_current",
        "fact_records",
        ["tenant_id", "fact_key"],
        unique=True,
        sqlite_where=sa.text("is_current = 1"),
    )
    # The old table-level UNIQUE(fact_key, version_n) has the same flaw and
    # cannot be dropped in place on SQLite, so rebuild the table without it.
    with op.batch_alter_table("fact_records", schema=None) as batch:
        batch.drop_constraint("uq_fact_records_key_version", type_="unique")
    op.create_index(
        "ux_fact_records_tenant_key_version",
        "fact_records",
        ["tenant_id", "fact_key", "version_n"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_fact_records_tenant_key_version", table_name="fact_records")
    with op.batch_alter_table("fact_records", schema=None) as batch:
        batch.create_unique_constraint(
            "uq_fact_records_key_version", ["fact_key", "version_n"]
        )
    op.drop_index("ux_fact_records_current", table_name="fact_records")
    op.create_index(
        "ux_fact_records_current",
        "fact_records",
        ["fact_key"],
        unique=True,
        sqlite_where=sa.text("is_current = 1"),
    )
