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
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "20260906_0030"
down_revision: str | None = "20260815_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAMES = ("sport", "sport_detail", "avg_hr_bpm", "max_hr_bpm", "calories", "distance_m")


def _columns() -> list[sa.Column[Any]]:
    return [
        sa.Column("sport", sa.Text(), nullable=True),
        sa.Column("sport_detail", sa.Text(), nullable=True),
        sa.Column("avg_hr_bpm", sa.Integer(), nullable=True),
        sa.Column("max_hr_bpm", sa.Integer(), nullable=True),
        sa.Column("calories", sa.Integer(), nullable=True),
        sa.Column("distance_m", sa.Float(), nullable=True),
    ]


def upgrade() -> None:
    for column in _columns():
        op.add_column("workout_sessions", column)


def downgrade() -> None:
    with op.batch_alter_table("workout_sessions", schema=None) as batch:
        for name in reversed(_NAMES):
            batch.drop_column(name)
