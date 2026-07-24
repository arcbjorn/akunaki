"""Durable deduplicated webhook inbox.

Revision ID: 20260723_0021
Revises: 20260722_0020
Create Date: 2026-07-23

Adds ``webhook_inbox``: one durable row per verified webhook delivery, unique on
``(connection_id, dedupe_key)`` so a redelivery is recognized as a duplicate and
never double-processed. ``body_payload_id`` is the **sole** FK between the inbox
and ``raw_payload`` (nullable; set after the payload is captured, per the design's
one-way body ownership). ``processing_status`` tracks the accept → enqueue →
process lifecycle.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260723_0021"
down_revision: str | None = "20260722_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_inbox",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("connection_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("delivery_id", sa.Text(), nullable=True),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("received_at", sa.Text(), nullable=False),
        sa.Column("verified_at", sa.Text(), nullable=False),
        sa.Column("headers_meta_json", sa.Text(), nullable=False),
        sa.Column("body_payload_id", sa.Text(), nullable=True),
        sa.Column("processing_status", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "processing_status IN ('accepted', 'enqueued', 'processed', 'ignored_dup')",
            name="webhook_inbox_status",
        ),
        sa.CheckConstraint(
            "json_valid(headers_meta_json)", name="webhook_inbox_headers_json"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["connections.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["body_payload_id"], ["raw_payload.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "connection_id", "dedupe_key", name="uq_webhook_inbox_dedupe"
        ),
    )


def downgrade() -> None:
    op.drop_table("webhook_inbox")
