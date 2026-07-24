"""Per-record slice body on raw revisions.

Revision ID: 20260719_0008
Revises: 20260719_0007
Create Date: 2026-07-19

A ``raw_revision`` identifies one logical **record**, but the transport page it
points at holds the whole collection. Without the record's own body, every
normalize job for a page would re-parse all of that page's records — wasted
work whose duplicate facts are only hidden by content-hash dedupe.

``slice_json`` stores the exact sub-body for the record. It is nullable so
revisions written before this migration remain readable; the reader falls back
to the full transport body for those.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260719_0008"
down_revision: str | None = "20260719_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "raw_revisions",
        sa.Column("slice_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("raw_revisions", "slice_json")
