"""Tests for versioned score persistence.

Cover the idempotent recompute (no-op on an identical dependency hash), the
supersede-on-change path, the null-score-iff-insufficient invariant, and the
signed factor rows. Scores are written through the real repository against a
migrated database.
"""

from __future__ import annotations

import itertools
from collections.abc import Generator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import DailyHealthScore, ScoreFactor, Tenant
from akunaki.adapters.db.score_repository import ScoreRepository, ScoreWriteOutcome
from akunaki.application.recovery_surface import RecoverySurface
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.recovery import RecoveryFactor, RecoveryStatus

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
DAY = "2026-07-20"

_IDS = itertools.count(1)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "scores.db"
    url = f"sqlite+libsql:///{db_path.resolve()}"
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", url)
    clear_settings_cache()
    cfg = Config(str(_backend_root() / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(_backend_root() / "alembic"))
    command.upgrade(cfg, "head")
    yield url
    clear_settings_cache()


@pytest.fixture
def factory(db_url: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=db_url))
    session_factory = create_session_factory(engine)
    with session_factory() as session, session.begin():
        session.add(
            Tenant(
                id="tenant-1",
                created_at="2026-07-01T00:00:00Z",
                status="active",
                primary_timezone="UTC",
                display_name="Test",
            )
        )
    try:
        yield session_factory
    finally:
        engine.dispose()


def _surface(
    *,
    status: RecoveryStatus = RecoveryStatus.PARTIAL,
    score: int | None = 72,
    confidence: float = 0.7,
    available_weight: float = 0.6,
    factors: tuple[RecoveryFactor, ...] = (),
) -> RecoverySurface:
    return RecoverySurface(
        local_health_day=DAY,
        score_code="recovery",
        status=status,
        score=score,
        confidence=confidence,
        available_weight=available_weight,
        factors=factors,
        data_gaps=(),
        formula_version="general_recovery_v0.1.0",
    )


def _factor_id() -> str:
    return f"factor-{next(_IDS)}"


def _write(
    factory: sessionmaker[Session], surface: RecoverySurface, *, score_id: str
) -> ScoreWriteOutcome:
    return ScoreRepository(factory).write_recovery_score(
        score_id=score_id,
        tenant_id="tenant-1",
        surface=surface,
        new_factor_id=_factor_id,
        as_of_at=T0,
        now=T0,
    )


def test_first_write_creates_version_one(factory: sessionmaker[Session]) -> None:
    outcome = _write(factory, _surface(), score_id="s1")
    assert outcome.is_new_version is True
    assert outcome.version_n == 1

    with factory() as session:
        row = session.scalars(select(DailyHealthScore)).one()
    assert row.score == 72
    assert row.status == "partial"
    assert row.is_current == 1
    assert row.dependency_hash != ""


def test_identical_recompute_is_a_noop(factory: sessionmaker[Session]) -> None:
    _write(factory, _surface(), score_id="s1")
    outcome = _write(factory, _surface(), score_id="s2")
    assert outcome.is_new_version is False
    assert outcome.version_n == 1

    with factory() as session:
        rows = session.scalars(select(DailyHealthScore)).all()
    assert len(rows) == 1  # no second version written


def test_changed_result_supersedes(factory: sessionmaker[Session]) -> None:
    _write(factory, _surface(score=72), score_id="s1")
    outcome = _write(factory, _surface(score=80), score_id="s2")
    assert outcome.is_new_version is True
    assert outcome.version_n == 2
    assert outcome.superseded_id == "s1"

    with factory() as session:
        rows = session.scalars(select(DailyHealthScore).order_by(DailyHealthScore.version_n)).all()
    assert len(rows) == 2
    assert rows[0].is_current == 0
    assert rows[0].superseded_by == "s2"
    assert rows[1].is_current == 1
    assert rows[1].score == 80


def test_insufficient_score_is_null(factory: sessionmaker[Session]) -> None:
    surface = _surface(status=RecoveryStatus.INSUFFICIENT, score=None, confidence=0.0)
    _write(factory, surface, score_id="s1")
    with factory() as session:
        row = session.scalars(select(DailyHealthScore)).one()
    assert row.status == "insufficient"
    assert row.score is None


def test_factors_persist_with_signs(factory: sessionmaker[Session]) -> None:
    factors = (
        RecoveryFactor(factor_code="hrv", present=True, weight=0.25, magnitude=80.0),
        RecoveryFactor(factor_code="resting_hr", present=True, weight=0.15, magnitude=30.0),
        RecoveryFactor(factor_code="temperature", present=False, weight=0.10, magnitude=0.0),
    )
    _write(factory, _surface(factors=factors), score_id="s1")

    with factory() as session:
        rows = {f.factor_code: f for f in session.scalars(select(ScoreFactor)).all()}
    assert rows["hrv"].sign == 1  # above midpoint -> pushes recovery up
    assert rows["hrv"].present == 1
    assert rows["resting_hr"].sign == -1  # below midpoint -> pushes down
    assert rows["temperature"].sign == 0  # absent -> neutral
    assert rows["temperature"].present == 0


def test_current_recovery_score_reads_back(factory: sessionmaker[Session]) -> None:
    _write(factory, _surface(score=65), score_id="s1")
    row = ScoreRepository(factory).current_recovery_score(
        tenant_id="tenant-1", local_health_day=DAY
    )
    assert row is not None
    assert row.score == 65


def test_new_current_replaces_old_in_reader(factory: sessionmaker[Session]) -> None:
    _write(factory, _surface(score=65), score_id="s1")
    _write(factory, _surface(score=90), score_id="s2")
    row = ScoreRepository(factory).current_recovery_score(
        tenant_id="tenant-1", local_health_day=DAY
    )
    assert row is not None
    assert row.score == 90


def test_read_with_factors_reconstructs_the_disclosure(
    factory: sessionmaker[Session],
) -> None:
    factors = (
        RecoveryFactor(factor_code="hrv", present=True, weight=0.25, magnitude=80.0),
        RecoveryFactor(factor_code="sleep_adherence", present=True, weight=0.20, magnitude=90.0),
        RecoveryFactor(factor_code="resting_hr", present=False, weight=0.15, magnitude=0.0),
    )
    _write(factory, _surface(score=77, available_weight=0.45, factors=factors), score_id="s1")

    stored = ScoreRepository(factory).current_recovery_with_factors(
        tenant_id="tenant-1", local_health_day=DAY
    )
    assert stored is not None
    assert stored.score == 77
    assert stored.version_n == 1
    assert stored.freshness_at is not None
    by_code = {f.factor_code: f for f in stored.factors}
    assert by_code["hrv"].present is True
    assert by_code["resting_hr"].present is False


def test_read_with_factors_is_none_when_absent(factory: sessionmaker[Session]) -> None:
    stored = ScoreRepository(factory).current_recovery_with_factors(
        tenant_id="tenant-1", local_health_day=DAY
    )
    assert stored is None
