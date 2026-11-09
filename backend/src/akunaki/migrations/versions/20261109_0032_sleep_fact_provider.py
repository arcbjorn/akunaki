"""Relabel sleep facts with the provider their schema version names.

Revision ID: 20261109_0032
Revises: 20261108_0031
Create Date: 2026-11-09

``write_sleep_fact`` stamped every sleep fact ``provider = 'oura'`` whatever
connection it came from, so the sleep that Google Health relays from a band
posed as ring data and outranked it in source selection. The writer now
derives the provider from the schema version, as the activity writer does;
this repairs the rows written before it did. The downgrade restores the old
label, which is what the old code would have written.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261109_0032"
down_revision: str | None = "20261108_0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GOOGLE_SLEEP = "entity_type = 'sleep_session' AND schema_version LIKE 'google_health%'"


def upgrade() -> None:
    op.execute(f"UPDATE fact_records SET provider = 'google_health' WHERE {_GOOGLE_SLEEP}")


def downgrade() -> None:
    op.execute(f"UPDATE fact_records SET provider = 'oura' WHERE {_GOOGLE_SLEEP}")
