"""Carry what the vendor measured about a workout beyond the zones.

Revision ID: 20260906_0030
Revises: 20260815_0029
Create Date: 2026-09-06

Adds ``workout_sessions.sport``, ``sport_detail``, ``avg_hr_bpm``,
``max_hr_bpm``, ``calories``, and ``distance_m``. All nullable: rows written
before this revision carry NULL until their raw revision is normalized again,
and a provider may report none of them.

``sport`` exists because a consumer routing sessions by kind (a swim to the
swim log, an indoor session to cardio) had only the clock to go by. Plain
``ADD COLUMN`` is enough on SQLite for nullable columns with no CHECK.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260906_0030"
down_revision: str | None = "20260815_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[object]], ...] = (
    ("sport", sa.Text()),
    ("sport_detail", sa.Text()),
    ("avg_hr_bpm", sa.Integer()),
    ("max_hr_bpm", sa.Integer()),
    ("calories", sa.Integer()),
    ("distance_m", sa.Float()),
)


def upgrade() -> None:
    for name, kind in _COLUMNS:
        op.add_column("workout_sessions", sa.Column(name, kind, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("workout_sessions", schema=None) as batch:
        for name, _kind in reversed(_COLUMNS):
            batch.drop_column(name)
