"""Tests for anomaly interval persistence."""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.anomaly_repository import AnomalyRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import Anomaly as AnomalyRow
from akunaki.adapters.db.models import Tenant
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.anomalies import AnomalySeverity

T0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
DAY = "2026-07-22"


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "anomalies.db"
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
