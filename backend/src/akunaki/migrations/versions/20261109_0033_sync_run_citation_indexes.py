"""Index the sync-run citations on raw pages and revisions.

Revision ID: 20261109_0033
Revises: 20261109_0032
Create Date: 2026-11-09

Sync-run retention keeps a run while any ``raw_payload`` or ``raw_revisions``
row cites it. Neither citing column was indexed, so the hourly sweep answered
"is this run cited?" by scanning every page for every run — tens of thousands
of rows each way on the production store.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261109_0033"
down_revision: str | None = "20261109_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_raw_payload_sync_run_id", "raw_payload", ["sync_run_id"])
    op.create_index("ix_raw_revisions_sync_run_id", "raw_revisions", ["sync_run_id"])


def downgrade() -> None:
    op.drop_index("ix_raw_revisions_sync_run_id", table_name="raw_revisions")
    op.drop_index("ix_raw_payload_sync_run_id", table_name="raw_payload")
