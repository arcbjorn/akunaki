"""Sync transport and logical raw layer.

Revision ID: 20260719_0006
Revises: 20260718_0005
Create Date: 2026-07-19

Adds the ingestion transport layer: ``sync_runs`` (one fetch execution),
``raw_payload`` (exact vendor bodies, **every** response retained),
``sync_cursors`` (per-stream progress), and the append-only logical layer
``raw_objects`` / ``raw_revisions``.

Design points enforced here rather than in application code:

- ``raw_payload.content_hash`` is **indexed, not unique**: a repeated response
  always writes a new transport row, and logical dedupe happens one layer up.
- ``raw_payload.sync_run_id`` is **nullable** so a webhook body capture can land
  before any sync run exists.
- ``raw_revisions`` is append-only with a monotonic ``revision_n`` unique per
  object; ``tombstone_reason`` is restricted to vendor/privacy deletions so
  ``superseded`` can never be recorded as a tombstone.
- The FK between the inbox and payload is one-way by design; ``webhook_inbox``
  is deliberately **not** created here (no webhook handling yet), which also
  avoids a bidirectional FK cycle.

Does not rewrite revisions 0001 through 0005.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260719_0006"
down_revision: str | None = "20260718_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- sync_runs -------------------------------------------------------------
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("connection_id", sa.Text(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("stream", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("finished_at", sa.Text(), nullable=True),
        sa.Column("error_class", sa.Text(), nullable=True),
        sa.Column("stats_json", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "trigger IN ('schedule', 'webhook', 'manual', 'reconcile', 'initial')",
            name="sync_run_trigger",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'partial')",
            name="sync_run_status",
        ),
        sa.CheckConstraint(
            "stats_json IS NULL OR json_valid(stats_json)",
            name="sync_run_stats_json_valid",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["connection_id"], ["connections.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_sync_runs_connection_started_at",
        "sync_runs",
        ["connection_id", "started_at"],
        unique=False,
    )

    # -- raw_payload -----------------------------------------------------------
    # Exact vendor bodies. Every response is retained, including identical
    # content on retries: content_hash is indexed for lookup, never unique.
    op.create_table(
        "raw_payload",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("connection_id", sa.Text(), nullable=False),
        sa.Column("sync_run_id", sa.Text(), nullable=True),
        sa.Column("transport_kind", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("stream", sa.Text(), nullable=False),
        sa.Column("page_token", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.Text(), nullable=True),
        sa.Column("received_at", sa.Text(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column("payload_blob", sa.LargeBinary(), nullable=True),
        sa.Column("request_meta_json", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "transport_kind IN ('sync_fetch', 'webhook_capture')",
            name="raw_payload_transport_kind",
        ),
        sa.CheckConstraint(
            "provider IN ('oura', 'google_health', 'polar')",
            name="raw_payload_provider",
        ),
        # A body may be absent, but never stored in both representations.
        sa.CheckConstraint(
            "NOT (payload_json IS NOT NULL AND payload_blob IS NOT NULL)",
            name="raw_payload_body_exclusive",
        ),
        sa.CheckConstraint(
            "payload_json IS NULL OR json_valid(payload_json)",
            name="raw_payload_json_valid",
        ),
        sa.CheckConstraint(
            "json_valid(request_meta_json)",
            name="raw_payload_request_meta_json_valid",
        ),
        sa.CheckConstraint("length(content_hash) > 0", name="raw_payload_content_hash_nonempty"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["connection_id"], ["connections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sync_run_id"], ["sync_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_raw_payload_connection_content_hash",
        "raw_payload",
        ["connection_id", "content_hash"],
        unique=False,
    )
    op.create_index(
        "ix_raw_payload_tenant_received_at",
        "raw_payload",
        ["tenant_id", "received_at"],
        unique=False,
    )

    # -- sync_cursors ----------------------------------------------------------
    op.create_table(
        "sync_cursors",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("connection_id", sa.Text(), nullable=False),
        sa.Column("stream", sa.Text(), nullable=False),
        sa.Column("cursor_type", sa.Text(), nullable=False),
        sa.Column("cursor_value", sa.Text(), nullable=False),
        sa.Column("window_start", sa.Text(), nullable=True),
        sa.Column("window_end", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "cursor_type IN ('timestamp', 'page_token', 'resource_id')",
            name="sync_cursor_type",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["connection_id"], ["connections.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connection_id", "stream", name="uq_sync_cursors_connection_stream"),
    )

    # -- raw_objects -----------------------------------------------------------
    # current_revision_id intentionally has no FK: raw_revisions references
    # raw_objects, and adding the reverse FK would create a circular dependency
    # that SQLite cannot satisfy on insert.
    op.create_table(
        "raw_objects",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("connection_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("stream", sa.Text(), nullable=False),
        sa.Column("vendor_record_id", sa.Text(), nullable=False),
        sa.Column("current_revision_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "provider IN ('oura', 'google_health', 'polar')",
            name="raw_object_provider",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["connection_id"], ["connections.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "stream",
            "vendor_record_id",
            name="uq_raw_objects_identity",
        ),
    )

    # -- raw_revisions ---------------------------------------------------------
    # Append-only. No normalizer_version here by design: raw rows are immutable
    # transport/logical snapshots; normalizer_version belongs on facts.
    op.create_table(
        "raw_revisions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("raw_object_id", sa.Text(), nullable=False),
        sa.Column("raw_payload_id", sa.Text(), nullable=False),
        sa.Column("sync_run_id", sa.Text(), nullable=True),
        sa.Column("revision_n", sa.Integer(), nullable=False),
        sa.Column("vendor_record_id", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.Text(), nullable=True),
        sa.Column("effective_at", sa.Text(), nullable=True),
        sa.Column("received_at", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("deletion_state", sa.Text(), nullable=False),
        sa.Column("is_tombstone", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("tombstone_reason", sa.Text(), nullable=True),
        sa.CheckConstraint("revision_n >= 1", name="raw_revision_n_pos"),
        sa.CheckConstraint(
            "deletion_state IN ('active', 'vendor_deleted', 'privacy_scrubbed')",
            name="raw_revision_deletion_state",
        ),
        sa.CheckConstraint("is_tombstone IN (0, 1)", name="raw_revision_is_tombstone_bool"),
        # 'superseded' is explicitly not a tombstone reason: superseding is
        # expressed by a later revision, not by marking the old one deleted.
        sa.CheckConstraint(
            "tombstone_reason IS NULL OR tombstone_reason IN ('vendor_deleted', 'privacy_delete')",
            name="raw_revision_tombstone_reason",
        ),
        sa.CheckConstraint(
            "(is_tombstone = 0 AND tombstone_reason IS NULL) OR "
            "(is_tombstone = 1 AND tombstone_reason IS NOT NULL)",
            name="raw_revision_tombstone_pair",
        ),
        sa.CheckConstraint("length(content_hash) > 0", name="raw_revision_content_hash_nonempty"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["raw_object_id"], ["raw_objects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["raw_payload_id"], ["raw_payload.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["sync_run_id"], ["sync_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("raw_object_id", "revision_n", name="uq_raw_revisions_object_n"),
    )
    op.create_index(
        "ix_raw_revisions_object_content_hash",
        "raw_revisions",
        ["raw_object_id", "content_hash"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_raw_revisions_object_content_hash", table_name="raw_revisions")
    op.drop_table("raw_revisions")
    op.drop_table("raw_objects")
    op.drop_table("sync_cursors")
    op.drop_index("ix_raw_payload_tenant_received_at", table_name="raw_payload")
    op.drop_index("ix_raw_payload_connection_content_hash", table_name="raw_payload")
    op.drop_table("raw_payload")
    op.drop_index("ix_sync_runs_connection_started_at", table_name="sync_runs")
    op.drop_table("sync_runs")
