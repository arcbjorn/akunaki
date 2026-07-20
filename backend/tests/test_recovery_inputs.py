"""Tests for windowed sleep-feature queries and recovery-input assembly.

Facts are seeded directly as ORM rows so the tests own the exact window shape.
They cover the efficiency query's "both-known" rule and the honest outcome that
sleep-only data yields an insufficient recovery evaluation.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.fact_repository import FactRepository
from akunaki.adapters.db.models import FactRecord, OvernightVitals, SleepSession, Tenant
from akunaki.application.recovery_inputs import RecoveryInputService
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339
from akunaki.domain.recovery import ComponentCode, RecoveryStatus, evaluate_recovery

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
TARGET_DAY = "2026-07-20"


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "recovery_inputs.db"
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


def _seed_session(
    factory: sessionmaker[Session],
    *,
    day: str,
    duration_min: float,
    time_in_bed_min: float | None,
    fact_id: str,
    tenant_id: str = "tenant-1",
) -> None:
    with factory() as session, session.begin():
        session.add(
            FactRecord(
                id=fact_id,
                tenant_id=tenant_id,
                connection_id=None,
                provider="oura",
                entity_type="sleep_session",
                vendor_record_id=fact_id,
                origin=None,
                method="wearable",
                utc_instant=NOW_S,
                start_utc=NOW_S,
                end_utc=NOW_S,
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
                normalizer_version="sleep_v0.1.0",
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
                tenant_id=tenant_id,
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


# ---------------------------------------------------------------------------
# Efficiency query
# ---------------------------------------------------------------------------


def test_efficiency_is_duration_over_in_bed(factory: sessionmaker[Session]) -> None:
    _seed_session(factory, day=TARGET_DAY, duration_min=432.0, time_in_bed_min=480.0, fact_id="e1")
    result = FactRepository(factory).daily_sleep_efficiency(
        tenant_id="tenant-1", local_health_days=[TARGET_DAY]
    )
    assert result[TARGET_DAY] == pytest.approx(432.0 / 480.0 * 100.0)  # 90.0


def test_efficiency_sums_sessions_before_dividing(
    factory: sessionmaker[Session],
) -> None:
    # Two sessions on one day: (400 + 20) / (440 + 40) * 100.
    _seed_session(factory, day=TARGET_DAY, duration_min=400.0, time_in_bed_min=440.0, fact_id="a")
    _seed_session(factory, day=TARGET_DAY, duration_min=20.0, time_in_bed_min=40.0, fact_id="b")
    result = FactRepository(factory).daily_sleep_efficiency(
        tenant_id="tenant-1", local_health_days=[TARGET_DAY]
    )
    assert result[TARGET_DAY] == pytest.approx(420.0 / 480.0 * 100.0)


def test_efficiency_omits_day_with_any_missing_in_bed(
    factory: sessionmaker[Session],
) -> None:
    # One session has no in-bed minutes: efficiency undefined for the whole day.
    _seed_session(factory, day=TARGET_DAY, duration_min=400.0, time_in_bed_min=440.0, fact_id="a")
    _seed_session(factory, day=TARGET_DAY, duration_min=20.0, time_in_bed_min=None, fact_id="b")
    result = FactRepository(factory).daily_sleep_efficiency(
        tenant_id="tenant-1", local_health_days=[TARGET_DAY]
    )
    assert TARGET_DAY not in result


# ---------------------------------------------------------------------------
# Input assembly
# ---------------------------------------------------------------------------


def test_adherence_present_when_duration_known(
    factory: sessionmaker[Session],
) -> None:
    _seed_session(factory, day=TARGET_DAY, duration_min=420.0, time_in_bed_min=None, fact_id="d")
    components = RecoveryInputService(features=FactRepository(factory)).recovery_components(
        tenant_id="tenant-1", local_health_day=TARGET_DAY
    )
    codes = {c.code for c in components}
    assert ComponentCode.SLEEP_ADHERENCE in codes
    # No efficiency (in-bed missing) and no baseline history either.
    assert ComponentCode.SLEEP_EFFICIENCY not in codes


def test_efficiency_component_omitted_without_mature_baseline(
    factory: sessionmaker[Session],
) -> None:
    # Efficiency known today but no prior series -> baseline insufficient -> omit.
    _seed_session(factory, day=TARGET_DAY, duration_min=432.0, time_in_bed_min=480.0, fact_id="t")
    components = RecoveryInputService(features=FactRepository(factory)).recovery_components(
        tenant_id="tenant-1", local_health_day=TARGET_DAY
    )
    codes = {c.code for c in components}
    assert ComponentCode.SLEEP_ADHERENCE in codes
    assert ComponentCode.SLEEP_EFFICIENCY not in codes


def test_efficiency_component_present_with_mature_baseline(
    factory: sessionmaker[Session],
) -> None:
    # Seed 28 prior days of efficiency plus today, then expect the component.
    for offset in range(1, 29):
        day = (datetime.fromisoformat(TARGET_DAY) - timedelta(days=offset)).date().isoformat()
        _seed_session(
            factory,
            day=day,
            duration_min=430.0,
            time_in_bed_min=480.0,
            fact_id=f"p-{offset}",
        )
    _seed_session(
        factory, day=TARGET_DAY, duration_min=460.0, time_in_bed_min=480.0, fact_id="today"
    )
    components = RecoveryInputService(features=FactRepository(factory)).recovery_components(
        tenant_id="tenant-1", local_health_day=TARGET_DAY
    )
    codes = {c.code for c in components}
    assert ComponentCode.SLEEP_EFFICIENCY in codes


def test_sleep_only_recovery_is_insufficient(
    factory: sessionmaker[Session],
) -> None:
    # Even with a mature efficiency baseline, there is no HRV or RHR, so the
    # recovery gate fails: the honest outcome is insufficient, not a score.
    for offset in range(1, 29):
        day = (datetime.fromisoformat(TARGET_DAY) - timedelta(days=offset)).date().isoformat()
        _seed_session(
            factory,
            day=day,
            duration_min=430.0,
            time_in_bed_min=480.0,
            fact_id=f"p-{offset}",
        )
    _seed_session(
        factory, day=TARGET_DAY, duration_min=460.0, time_in_bed_min=480.0, fact_id="today"
    )
    components = RecoveryInputService(features=FactRepository(factory)).recovery_components(
        tenant_id="tenant-1", local_health_day=TARGET_DAY
    )
    result = evaluate_recovery(components)
    assert result.status is RecoveryStatus.INSUFFICIENT
    assert result.score is None


def test_no_sleep_yields_no_components(factory: sessionmaker[Session]) -> None:
    components = RecoveryInputService(features=FactRepository(factory)).recovery_components(
        tenant_id="tenant-1", local_health_day=TARGET_DAY
    )
    assert components == []


# ---------------------------------------------------------------------------
# Overnight vitals: the path to a real score
# ---------------------------------------------------------------------------


def _seed_vitals(
    factory: sessionmaker[Session],
    *,
    day: str,
    fact_id: str,
    hrv_ms: float | None = 60.0,
    resting_hr_bpm: float | None = 50.0,
) -> None:
    with factory() as session, session.begin():
        session.add(
            FactRecord(
                id=fact_id,
                tenant_id="tenant-1",
                connection_id=None,
                provider="oura",
                entity_type="overnight_vitals",
                vendor_record_id=fact_id,
                origin=None,
                method="wearable",
                utc_instant=NOW_S,
                start_utc=NOW_S,
                end_utc=NOW_S,
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
                normalizer_version="oura_vitals_v0.1.0",
                content_hash=fact_id,
                fact_key=f"overnight_vitals:{fact_id}",
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
            OvernightVitals(
                fact_record_id=fact_id,
                tenant_id="tenant-1",
                hrv_ms=hrv_ms,
                resting_hr_bpm=resting_hr_bpm,
            )
        )


def test_hrv_query_omits_days_with_no_reading(factory: sessionmaker[Session]) -> None:
    _seed_vitals(factory, day=TARGET_DAY, fact_id="v1", hrv_ms=55.0, resting_hr_bpm=None)
    repo = FactRepository(factory)
    hrv = repo.daily_hrv(tenant_id="tenant-1", local_health_days=[TARGET_DAY])
    rhr = repo.daily_resting_hr(tenant_id="tenant-1", local_health_days=[TARGET_DAY])
    assert hrv[TARGET_DAY] == 55.0
    assert TARGET_DAY not in rhr  # this fact carries no RHR


def test_hrv_and_rhr_components_appear_with_mature_baseline(
    factory: sessionmaker[Session],
) -> None:
    for offset in range(1, 29):
        day = (datetime.fromisoformat(TARGET_DAY) - timedelta(days=offset)).date().isoformat()
        _seed_vitals(factory, day=day, fact_id=f"pv-{offset}", hrv_ms=60.0, resting_hr_bpm=50.0)
    _seed_vitals(factory, day=TARGET_DAY, fact_id="tv", hrv_ms=70.0, resting_hr_bpm=46.0)
    _seed_session(factory, day=TARGET_DAY, duration_min=460.0, time_in_bed_min=None, fact_id="ts")

    components = RecoveryInputService(features=FactRepository(factory)).recovery_components(
        tenant_id="tenant-1", local_health_day=TARGET_DAY
    )
    codes = {c.code for c in components}
    assert ComponentCode.HRV in codes
    assert ComponentCode.RESTING_HR in codes
    assert ComponentCode.SLEEP_ADHERENCE in codes


def test_full_coverage_reaches_a_real_score(factory: sessionmaker[Session]) -> None:
    # Adherence (0.20) + HRV (0.25) + RHR (0.15) = 0.60 available weight, which
    # clears the gate: the previously insufficient tenant now gets a score.
    for offset in range(1, 29):
        day = (datetime.fromisoformat(TARGET_DAY) - timedelta(days=offset)).date().isoformat()
        _seed_vitals(factory, day=day, fact_id=f"pv-{offset}", hrv_ms=60.0, resting_hr_bpm=50.0)
    _seed_vitals(factory, day=TARGET_DAY, fact_id="tv", hrv_ms=62.0, resting_hr_bpm=49.0)
    _seed_session(factory, day=TARGET_DAY, duration_min=470.0, time_in_bed_min=None, fact_id="ts")

    components = RecoveryInputService(features=FactRepository(factory)).recovery_components(
        tenant_id="tenant-1", local_health_day=TARGET_DAY
    )
    result = evaluate_recovery(components)
    assert result.status is not RecoveryStatus.INSUFFICIENT
    assert result.score is not None
    assert 0 <= result.score <= 100
