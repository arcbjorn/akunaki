"""Source-selection behavior of the sleep feature queries.

When more than one provider supplies sleep for the same local day, the fact
repository reads only the **authoritative** provider's sessions (Oura over
Google Health), never summing or blending them. These drive the real repository
against a migrated database.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.fact_repository import FactRepository
from akunaki.adapters.db.models import FactRecord, SleepSession, Tenant
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
DAY = "2026-07-20"


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "source_selection.db"
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
                created_at=NOW_S,
                status="active",
                primary_timezone="UTC",
                display_name="Test",
            )
        )
    try:
        yield session_factory
    finally:
        engine.dispose()


def _seed_sleep(
    factory: sessionmaker[Session],
    *,
    provider: str,
    fact_id: str,
    duration_min: float,
    time_in_bed_min: float | None,
    start_utc: str = NOW_S,
    day: str = DAY,
) -> None:
    with factory() as session, session.begin():
        session.add(
            FactRecord(
                id=fact_id,
                tenant_id="tenant-1",
                connection_id=None,
                provider=provider,
                entity_type="sleep_session",
                vendor_record_id=fact_id,
                origin=None,
                method="wearable",
                utc_instant=start_utc,
                start_utc=start_utc,
                end_utc=start_utc,
                source_offset_minutes=0,
                iana_timezone="UTC",
                local_health_day=day,
                unit=None,
                quality="high",
                confidence=1.0,
                freshness_at=NOW_S,
                raw_revision_id=None,
                raw_payload_id=None,
                schema_version="v1",
                normalizer_version="n",
                content_hash=fact_id,
                fact_key=f"sleep_session:{fact_id}",
                version_n=1,
                is_current=1,
                superseded_by=None,
                superseded_at=None,
                deletion_state="active",
                exclude_from_load=0,
                created_at=NOW_S,
            )
        )
        session.add(
            SleepSession(
                fact_record_id=fact_id,
                tenant_id="tenant-1",
                is_nap=0,
                duration_min=duration_min,
                time_in_bed_min=time_in_bed_min,
                efficiency_pct=None,
                light_min=None,
                deep_min=None,
                rem_min=None,
                awake_min=None,
            )
        )


def test_duration_uses_oura_over_google_health(factory: sessionmaker[Session]) -> None:
    # Both providers cover the night; the sum must NOT double-count.
    _seed_sleep(factory, provider="oura", fact_id="o", duration_min=420.0, time_in_bed_min=460.0)
    _seed_sleep(
        factory, provider="google_health", fact_id="g", duration_min=400.0, time_in_bed_min=440.0
    )

    durations = FactRepository(factory).daily_sleep_durations(
        tenant_id="tenant-1", local_health_days=[DAY]
    )
    # Oura wins: 420, not 820 (sum) and not 400 (Google).
    assert durations[DAY] == pytest.approx(420.0)


def test_duration_falls_back_to_google_health(factory: sessionmaker[Session]) -> None:
    _seed_sleep(
        factory, provider="google_health", fact_id="g", duration_min=400.0, time_in_bed_min=440.0
    )
    durations = FactRepository(factory).daily_sleep_durations(
        tenant_id="tenant-1", local_health_days=[DAY]
    )
    assert durations[DAY] == pytest.approx(400.0)


def test_efficiency_uses_only_the_authoritative_provider(
    factory: sessionmaker[Session],
) -> None:
    # Oura defines efficiency; Google Health's differing ratio is not mixed in.
    _seed_sleep(factory, provider="oura", fact_id="o", duration_min=440.0, time_in_bed_min=460.0)
    _seed_sleep(
        factory, provider="google_health", fact_id="g", duration_min=300.0, time_in_bed_min=600.0
    )
    eff = FactRepository(factory).daily_sleep_efficiency(
        tenant_id="tenant-1", local_health_days=[DAY]
    )
    # 440 / 460 * 100, not a blend with Google's 300/600.
    assert eff[DAY] == pytest.approx(440.0 / 460.0 * 100.0)


def test_efficiency_omitted_when_authoritative_provider_lacks_in_bed(
    factory: sessionmaker[Session],
) -> None:
    # Oura is authoritative but has no in-bed minutes -> undefined; Google's
    # complete data does NOT rescue the day (no cross-provider fallback).
    _seed_sleep(factory, provider="oura", fact_id="o", duration_min=440.0, time_in_bed_min=None)
    _seed_sleep(
        factory, provider="google_health", fact_id="g", duration_min=300.0, time_in_bed_min=600.0
    )
    eff = FactRepository(factory).daily_sleep_efficiency(
        tenant_id="tenant-1", local_health_days=[DAY]
    )
    assert DAY not in eff


def test_midpoint_uses_only_the_authoritative_provider(
    factory: sessionmaker[Session],
) -> None:
    # A longer Google-Health session must not become the principal one over
    # Oura's shorter authoritative session.
    _seed_sleep(
        factory,
        provider="oura",
        fact_id="o",
        duration_min=420.0,
        time_in_bed_min=460.0,
        start_utc="2026-07-19T23:00:00Z",
    )
    _seed_sleep(
        factory,
        provider="google_health",
        fact_id="g",
        duration_min=600.0,
        time_in_bed_min=620.0,
        start_utc="2026-07-19T20:00:00Z",
    )
    mids = FactRepository(factory).daily_principal_sleep_midpoint(
        tenant_id="tenant-1", local_health_days=[DAY]
    )
    # Oura onset 23:00 (1380 min) + duration/2 (210) = 1590, wrapped to 150 on
    # the [0, 1440) circle. Google's 20:00 onset would give a different midpoint;
    # it must not be chosen.
    assert mids[DAY] == pytest.approx((1380.0 + 210.0) % 1440.0)
