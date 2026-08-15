"""Tests for anomaly interval persistence."""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.anomaly_repository import AnomalyRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import Anomaly as AnomalyRow
from akunaki.adapters.db.models import Tenant
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.anomalies import AnomalySeverity
from conftest import upgrade_to_head

T0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
DAY = "2026-07-22"


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "anomalies.db"
    url = f"sqlite+libsql:///{db_path.resolve()}"
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", url)
    clear_settings_cache()
    upgrade_to_head(url)
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


def test_open_and_read_current_state(factory: sessionmaker[Session]) -> None:
    repo = AnomalyRepository(factory)
    assert repo.current_state(tenant_id="tenant-1", feature_code="low_hrv") is None

    repo.open_interval(
        anomaly_id="a1",
        tenant_id="tenant-1",
        feature_code="low_hrv",
        severity=AnomalySeverity.MODERATE,
        z_like=-2.7,
        formula_version="anomaly_v0.1.0",
        local_health_day=DAY,
        now=T0,
    )
    state = repo.current_state(tenant_id="tenant-1", feature_code="low_hrv")
    assert state is not None
    assert state.is_open is True
    assert state.severity is AnomalySeverity.MODERATE
    assert state.consecutive_clear_days == 0


def test_update_open_interval_bumps_clear_run(factory: sessionmaker[Session]) -> None:
    repo = AnomalyRepository(factory)
    repo.open_interval(
        anomaly_id="a1",
        tenant_id="tenant-1",
        feature_code="low_hrv",
        severity=AnomalySeverity.MODERATE,
        z_like=-2.7,
        formula_version="anomaly_v0.1.0",
        local_health_day=DAY,
        now=T0,
    )
    repo.update_open_interval(
        tenant_id="tenant-1",
        feature_code="low_hrv",
        severity=AnomalySeverity.HIGH,
        consecutive_clear_days=1,
        now=T0,
    )
    state = repo.current_state(tenant_id="tenant-1", feature_code="low_hrv")
    assert state is not None
    assert state.severity is AnomalySeverity.HIGH
    assert state.consecutive_clear_days == 1


def test_close_interval_deactivates(factory: sessionmaker[Session]) -> None:
    repo = AnomalyRepository(factory)
    repo.open_interval(
        anomaly_id="a1",
        tenant_id="tenant-1",
        feature_code="low_hrv",
        severity=AnomalySeverity.MODERATE,
        z_like=-2.7,
        formula_version="anomaly_v0.1.0",
        local_health_day=DAY,
        now=T0,
    )
    repo.close_interval(
        tenant_id="tenant-1",
        feature_code="low_hrv",
        local_health_day="2026-07-24",
        now=T0,
    )
    assert repo.current_state(tenant_id="tenant-1", feature_code="low_hrv") is None
    with factory() as session:
        row = session.scalars(select(AnomalyRow)).one()
    assert row.is_active == 0
    assert row.ended_on == "2026-07-24"


def test_reopen_after_close_is_allowed(factory: sessionmaker[Session]) -> None:
    # The partial unique index only covers active rows, so a closed interval
    # does not block a fresh one for the same feature.
    repo = AnomalyRepository(factory)
    repo.open_interval(
        anomaly_id="a1",
        tenant_id="tenant-1",
        feature_code="low_hrv",
        severity=AnomalySeverity.MODERATE,
        z_like=-2.7,
        formula_version="anomaly_v0.1.0",
        local_health_day=DAY,
        now=T0,
    )
    repo.close_interval(
        tenant_id="tenant-1", feature_code="low_hrv", local_health_day="2026-07-24", now=T0
    )
    repo.open_interval(
        anomaly_id="a2",
        tenant_id="tenant-1",
        feature_code="low_hrv",
        severity=AnomalySeverity.HIGH,
        z_like=-3.0,
        formula_version="anomaly_v0.1.0",
        local_health_day="2026-07-30",
        now=T0,
    )
    state = repo.current_state(tenant_id="tenant-1", feature_code="low_hrv")
    assert state is not None
    assert state.severity is AnomalySeverity.HIGH


def test_has_active_high_severity(factory: sessionmaker[Session]) -> None:
    repo = AnomalyRepository(factory)
    assert repo.has_active_high_severity(tenant_id="tenant-1") is False
    repo.open_interval(
        anomaly_id="a1",
        tenant_id="tenant-1",
        feature_code="low_hrv",
        severity=AnomalySeverity.MODERATE,
        z_like=-2.7,
        formula_version="anomaly_v0.1.0",
        local_health_day=DAY,
        now=T0,
    )
    assert repo.has_active_high_severity(tenant_id="tenant-1") is False
    repo.open_interval(
        anomaly_id="a2",
        tenant_id="tenant-1",
        feature_code="elevated_rhr",
        severity=AnomalySeverity.HIGH,
        z_like=3.0,
        formula_version="anomaly_v0.1.0",
        local_health_day=DAY,
        now=T0,
    )
    assert repo.has_active_high_severity(tenant_id="tenant-1") is True


def _open(
    repo: AnomalyRepository,
    *,
    anomaly_id: str,
    feature_code: str,
    day: str = DAY,
    severity: AnomalySeverity = AnomalySeverity.MODERATE,
) -> None:
    repo.open_interval(
        anomaly_id=anomaly_id,
        tenant_id="tenant-1",
        feature_code=feature_code,
        severity=severity,
        z_like=-2.7,
        formula_version="anomaly_v0.1.0",
        local_health_day=day,
        now=T0,
    )


def test_recent_intervals_reads_what_the_tracker_wrote(
    factory: sessionmaker[Session],
) -> None:
    """The read query must see the interval the write path opened."""
    repo = AnomalyRepository(factory)
    _open(repo, anomaly_id="a1", feature_code="low_hrv", severity=AnomalySeverity.HIGH)

    [interval] = repo.recent_intervals(tenant_id="tenant-1", since_day="2026-07-01")

    assert interval.feature_code == "low_hrv"
    assert interval.severity is AnomalySeverity.HIGH
    assert interval.started_on == DAY
    assert interval.ended_on is None
    assert interval.is_active is True
    assert interval.formula_version == "anomaly_v0.1.0"


def test_recent_intervals_keeps_an_active_one_older_than_the_window(
    factory: sessionmaker[Session],
) -> None:
    """An interval open since long ago is still what the user is living with.

    The window bounds how long a *cleared* anomaly stays visible; it must never
    hide one that is still open.
    """
    repo = AnomalyRepository(factory)
    _open(repo, anomaly_id="a1", feature_code="low_hrv", day="2020-01-01")

    intervals = repo.recent_intervals(tenant_id="tenant-1", since_day="2026-07-01")

    assert [i.feature_code for i in intervals] == ["low_hrv"]


def test_recent_intervals_respects_the_limit(factory: sessionmaker[Session]) -> None:
    """A bounded read: a pathological history cannot return unbounded rows."""
    repo = AnomalyRepository(factory)
    for n, code in enumerate(
        ("low_hrv", "elevated_rhr", "deviant_temperature", "short_sleep"), start=1
    ):
        _open(repo, anomaly_id=f"a{n}", feature_code=code)

    assert len(repo.recent_intervals(tenant_id="tenant-1", since_day="2026-07-01", limit=2)) == 2
