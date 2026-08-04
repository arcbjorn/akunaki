"""Tamper-evident audit events.

Revision ID: 20260804_0026
Revises: 20260803_0025
Create Date: 2026-08-04

Records *that* an action happened, to what, by whom — never a health value.
The security model names audit as the control for **repudiation** ("I didn't
delete that"), so the row has to be trustworthy in two ways:

- ``metadata_json`` is a bounded key/value map validated PHI-free before insert
  (``domain/audit.py``). An audit trail that logged measurements would be a
  second PHI store with looser access and longer retention.
- ``event_hash`` covers the row's content **and** ``previous_hash``, forming a
  chain per tenant: editing or removing a past event breaks every link after
  it. This detects row-level tampering; it does **not** defend against an
  attacker who can rewrite the whole chain, which would need signed batches.

``tenant_id`` is nullable for system-scoped actions (a scheduled sweep belongs
to no customer) and has **no FK to tenants**: like the deletion request, the
audit record must outlive the tenant it describes, or erasing a tenant would
erase the proof that the erasure happened.

``seq`` gives the chain a total order that does not depend on timestamp
resolution — two events in the same second still have an unambiguous
predecessor.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260804_0026"
down_revision: str | None = "20260803_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), autoincrement=True, nullable=False),
        # Deliberately no FK: the record outlives the tenant it describes.
        sa.Column("tenant_id", sa.Text(), nullable=True),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.Text(), nullable=False),
        sa.Column("resource_id", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("previous_hash", sa.Text(), nullable=False),
        sa.Column("event_hash", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "actor_type IN ('user', 'system', 'worker')", name="audit_actor_type"
        ),
        sa.CheckConstraint("json_valid(metadata_json)", name="audit_metadata_json_valid"),
        sa.CheckConstraint("length(event_hash) = 64", name="audit_event_hash_len"),
        sa.CheckConstraint("length(previous_hash) = 64", name="audit_previous_hash_len"),
        sa.PrimaryKeyConstraint("seq"),
        sa.UniqueConstraint("id", name="uq_audit_events_id"),
        # A repeated hash would mean two events claim the same chain position.
        sa.UniqueConstraint("event_hash", name="uq_audit_events_hash"),
    )
    op.create_index(
        "ix_audit_events_tenant_seq",
        "audit_events",
        ["tenant_id", "seq"],
    )
    op.create_index("ix_audit_events_action", "audit_events", ["action", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_action", table_name="audit_events")
    op.drop_index("ix_audit_events_tenant_seq", table_name="audit_events")
    op.drop_table("audit_events")
