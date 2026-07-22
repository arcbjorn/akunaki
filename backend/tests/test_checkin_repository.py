"""Tests for versioned subjective check-in persistence."""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.checkin_repository import CheckInRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import SubjectiveCheckIn, Tenant
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.subjective import SubjectiveInputs

T0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
DAY = "2026-07-22"


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "checkin.db"
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


def _inputs(*, energy: float = 0.6, stress: float = 0.4, symptom: float = 0.2) -> SubjectiveInputs:
    return SubjectiveInputs(energy_n=energy, stress_n=stress, symptom_burden_n=symptom)


def test_first_record_is_version_one(factory: sessionmaker[Session]) -> None:
    outcome = CheckInRepository(factory).record_check_in(
        check_in_id="c1",
        tenant_id="tenant-1",
        local_health_day=DAY,
        inputs=_inputs(),
        completed_at=T0,
        now=T0,
    )
    assert outcome.version_n == 1
    with factory() as session:
        row = session.scalars(select(SubjectiveCheckIn)).one()
    assert row.is_current == 1
    assert row.energy_n == 0.6
    assert row.completed_at is not None


def test_resubmission_supersedes(factory: sessionmaker[Session]) -> None:
    repo = CheckInRepository(factory)
    repo.record_check_in(
        check_in_id="c1",
        tenant_id="tenant-1",
        local_health_day=DAY,
        inputs=_inputs(energy=0.5),
        completed_at=T0,
        now=T0,
    )
    outcome = repo.record_check_in(
        check_in_id="c2",
        tenant_id="tenant-1",
        local_health_day=DAY,
        inputs=_inputs(energy=0.9),
        completed_at=T0,
        now=T0,
    )
    assert outcome.version_n == 2
    assert outcome.superseded_id == "c1"

    with factory() as session:
        rows = session.scalars(
            select(SubjectiveCheckIn).order_by(SubjectiveCheckIn.version_n)
        ).all()
    assert len(rows) == 2
    assert rows[0].is_current == 0
    assert rows[1].is_current == 1
    assert rows[1].energy_n == 0.9


def test_current_inputs_read_back(factory: sessionmaker[Session]) -> None:
    CheckInRepository(factory).record_check_in(
        check_in_id="c1",
        tenant_id="tenant-1",
        local_health_day=DAY,
        inputs=_inputs(energy=0.7, stress=0.3, symptom=0.0),
        completed_at=T0,
        now=T0,
    )
    inputs = CheckInRepository(factory).current_check_in_inputs(
        tenant_id="tenant-1", local_health_day=DAY
    )
    assert inputs is not None
    assert inputs.energy_n == 0.7
    assert inputs.symptom_burden_n == 0.0


def test_no_check_in_reads_none(factory: sessionmaker[Session]) -> None:
    assert (
        CheckInRepository(factory).current_check_in_inputs(
            tenant_id="tenant-1", local_health_day=DAY
        )
        is None
    )
