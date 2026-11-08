"""Track the last local day that advanced each anomaly interval.

Revision ID: 20261108_0031
Revises: 20260906_0030
Create Date: 2026-11-08

Adds ``anomalies.tracked_through``: the latest ``local_health_day`` whose
signal moved the interval. The tracker advanced its state machine for days in
whatever order recompute jobs ran — a backfill normalizes a month in arbitrary
order, and one day recomputes several times as its facts land — so an interval
opened on October 5 was closed by September 29 reading as normal, and a
partial morning opened anomalies the full day retracted. On the production
store 369 of roughly 400 rows ended before they started.

Those rows are deleted here: an interval that ends before it starts describes
nothing. Surviving rows are stamped with the last day they record —
``ended_on`` when closed, ``started_on`` when open. The downgrade drops the
column; the deleted rows were artifacts and are not restored.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261108_0031"
down_revision: str | None = "20260906_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("anomalies", sa.Column("tracked_through", sa.Text(), nullable=True))
    op.execute("DELETE FROM anomalies WHERE ended_on IS NOT NULL AND ended_on < started_on")
    op.execute("UPDATE anomalies SET tracked_through = COALESCE(ended_on, started_on)")


def downgrade() -> None:
    with op.batch_alter_table("anomalies", schema=None) as batch:
        batch.drop_column("tracked_through")
