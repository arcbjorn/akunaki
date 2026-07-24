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
from akunaki.adapters.db.models import (
    FactRecord,
    OvernightVitals,
    SleepSession,
    Tenant,
    WorkoutSession,
)
from akunaki.application.recovery_inputs import RecoveryInputService
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339
from akunaki.domain.recovery import ComponentCode, RecoveryStatus, evaluate_recovery
from akunaki.domain.subjective import SubjectiveInputs

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
    temperature_deviation_c: float | None = None,
    respiratory_rate_bpm: float | None = None,
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
                temperature_deviation_c=temperature_deviation_c,
                respiratory_rate_bpm=respiratory_rate_bpm,
            )
        )


def test_respiratory_component_appears_with_mature_baseline(
    factory: sessionmaker[Session],
) -> None:
    for offset in range(1, 29):
        day = (datetime.fromisoformat(TARGET_DAY) - timedelta(days=offset)).date().isoformat()
        _seed_vitals(factory, day=day, fact_id=f"pv-{offset}", respiratory_rate_bpm=14.0)
    # Today's rate is elevated vs a flat baseline -> component below 50.
    _seed_vitals(factory, day=TARGET_DAY, fact_id="tv", respiratory_rate_bpm=17.0)

    components = RecoveryInputService(features=FactRepository(factory)).recovery_components(
        tenant_id="tenant-1", local_health_day=TARGET_DAY
    )
    by_code = {c.code: c for c in components}
    assert ComponentCode.RESPIRATORY in by_code
    assert by_code[ComponentCode.RESPIRATORY].c < 50.0


def test_below_baseline_respiratory_is_not_rewarded(
    factory: sessionmaker[Session],
) -> None:
    for offset in range(1, 29):
        day = (datetime.fromisoformat(TARGET_DAY) - timedelta(days=offset)).date().isoformat()
        _seed_vitals(factory, day=day, fact_id=f"pv-{offset}", respiratory_rate_bpm=14.0)
    # A lower-than-baseline rate is not rewarded: the component stays at 50.
    _seed_vitals(factory, day=TARGET_DAY, fact_id="tv", respiratory_rate_bpm=11.0)

    components = RecoveryInputService(features=FactRepository(factory)).recovery_components(
        tenant_id="tenant-1", local_health_day=TARGET_DAY
    )
    resp = next(c for c in components if c.code is ComponentCode.RESPIRATORY)
    assert resp.c == pytest.approx(50.0)


def test_temperature_component_appears_with_mature_baseline(
    factory: sessionmaker[Session],
) -> None:
    for offset in range(1, 29):
        day = (datetime.fromisoformat(TARGET_DAY) - timedelta(days=offset)).date().isoformat()
        _seed_vitals(factory, day=day, fact_id=f"pv-{offset}", temperature_deviation_c=0.0)
    _seed_vitals(factory, day=TARGET_DAY, fact_id="tv", temperature_deviation_c=0.6)

    components = RecoveryInputService(features=FactRepository(factory)).recovery_components(
        tenant_id="tenant-1", local_health_day=TARGET_DAY
    )
    codes = {c.code for c in components}
    assert ComponentCode.TEMPERATURE in codes
    # A departure from a flat baseline lowers the temperature component below 50.
    temp = next(c for c in components if c.code is ComponentCode.TEMPERATURE)
    assert temp.c < 50.0


def test_temperature_query_omits_days_with_no_reading(factory: sessionmaker[Session]) -> None:
    _seed_vitals(factory, day=TARGET_DAY, fact_id="v1", temperature_deviation_c=None)
    temps = FactRepository(factory).daily_temperature_deviation(
        tenant_id="tenant-1", local_health_days=[TARGET_DAY]
    )
    assert TARGET_DAY not in temps


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


def _seed_session_at(
    factory: sessionmaker[Session],
    *,
    day: str,
    start_utc: str,
    duration_min: float,
    fact_id: str,
    is_nap: bool = False,
) -> None:
    """Seed a sleep session with an explicit onset instant (offset 0)."""
    with factory() as session, session.begin():
        session.add(
            FactRecord(
                id=fact_id,
                tenant_id="tenant-1",
                connection_id=None,
                provider="oura",
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
                tenant_id="tenant-1",
                is_nap=1 if is_nap else 0,
                duration_min=duration_min,
                time_in_bed_min=None,
                efficiency_pct=None,
                light_min=None,
                deep_min=None,
                rem_min=None,
                awake_min=None,
            )
        )


def test_consistency_component_appears_with_regular_sleep(
    factory: sessionmaker[Session],
) -> None:
    # Fourteen nights, each starting 23:00 UTC (offset 0) for 8h -> identical
    # 03:00 midpoints -> R = 1 -> consistency 100.
    for offset in range(14):
        day = (datetime.fromisoformat(TARGET_DAY) - timedelta(days=offset)).date().isoformat()
        prev = (datetime.fromisoformat(day) - timedelta(days=1)).date().isoformat()
        _seed_session_at(
            factory,
            day=day,
            start_utc=f"{prev}T23:00:00Z",
            duration_min=480.0,
            fact_id=f"s-{offset}",
        )

    components = RecoveryInputService(features=FactRepository(factory)).recovery_components(
        tenant_id="tenant-1", local_health_day=TARGET_DAY
    )
    by_code = {c.code: c for c in components}
    assert ComponentCode.SLEEP_CONSISTENCY in by_code
    assert by_code[ComponentCode.SLEEP_CONSISTENCY].c == pytest.approx(100.0)


def test_consistency_omitted_below_seven_valid_nights(
    factory: sessionmaker[Session],
) -> None:
    # Only six nights with a principal session: below the minimum -> omitted.
    for offset in range(6):
        day = (datetime.fromisoformat(TARGET_DAY) - timedelta(days=offset)).date().isoformat()
        prev = (datetime.fromisoformat(day) - timedelta(days=1)).date().isoformat()
        _seed_session_at(
            factory,
            day=day,
            start_utc=f"{prev}T23:00:00Z",
            duration_min=480.0,
            fact_id=f"s-{offset}",
        )

    components = RecoveryInputService(features=FactRepository(factory)).recovery_components(
        tenant_id="tenant-1", local_health_day=TARGET_DAY
    )
    assert ComponentCode.SLEEP_CONSISTENCY not in {c.code for c in components}


def test_naps_are_not_valid_nights_for_consistency(
    factory: sessionmaker[Session],
) -> None:
    # Seven nap-only days: no principal (non-nap) session, so no valid night.
    for offset in range(7):
        day = (datetime.fromisoformat(TARGET_DAY) - timedelta(days=offset)).date().isoformat()
        _seed_session_at(
            factory,
            day=day,
            start_utc=f"{day}T13:00:00Z",
            duration_min=40.0,
            fact_id=f"nap-{offset}",
            is_nap=True,
        )

    midpoints = FactRepository(factory).daily_principal_sleep_midpoint(
        tenant_id="tenant-1", local_health_days=[TARGET_DAY]
    )
    assert midpoints == {}


def test_prior_load_omitted_without_a_load_source(
    factory: sessionmaker[Session],
) -> None:
    # No daily-load data exists, so ACWR is undefined and the prior-load
    # component is omitted for every tenant.
    _seed_session(factory, day=TARGET_DAY, duration_min=420.0, time_in_bed_min=None, fact_id="ts")
    components = RecoveryInputService(features=FactRepository(factory)).recovery_components(
        tenant_id="tenant-1", local_health_day=TARGET_DAY
    )
    assert ComponentCode.PRIOR_LOAD_BALANCE not in {c.code for c in components}


def test_prior_load_present_when_load_is_fully_covered() -> None:
    # A fake feature source with full 7/28 load coverage produces a defined
    # ACWR and thus a present prior-load component.
    from akunaki.application.recovery_inputs import FeatureSource

    class _LoadOnly:
        def daily_sleep_durations(self, *, tenant_id: str, local_health_days: list[str]):
            return {}

        def daily_sleep_efficiency(self, *, tenant_id: str, local_health_days: list[str]):
            return {}

        def daily_hrv(self, *, tenant_id: str, local_health_days: list[str]):
            return {}

        def daily_resting_hr(self, *, tenant_id: str, local_health_days: list[str]):
            return {}

        def daily_temperature_deviation(self, *, tenant_id: str, local_health_days: list[str]):
            return {}

        def daily_respiratory_rate(self, *, tenant_id: str, local_health_days: list[str]):
            return {}

        def daily_principal_sleep_midpoint(self, *, tenant_id: str, local_health_days: list[str]):
            return {}

        def daily_strain_load(self, *, tenant_id: str, local_health_days: list[str]):
            # Every day in the 28-day window a known load of 100 -> balanced ACWR.
            return dict.fromkeys(local_health_days, 100.0)

    source: FeatureSource = _LoadOnly()
    components = RecoveryInputService(features=source).recovery_components(
        tenant_id="tenant-1", local_health_day=TARGET_DAY
    )
    by_code = {c.code: c for c in components}
    assert ComponentCode.PRIOR_LOAD_BALANCE in by_code
    # acute 700, chronic weekly 700 -> ACWR 1.0 -> balance band -> c = 100.
    assert by_code[ComponentCode.PRIOR_LOAD_BALANCE].c == pytest.approx(100.0)


def _seed_workout(
    factory: sessionmaker[Session],
    *,
    day: str,
    fact_id: str,
    session_load: float = 100.0,
) -> None:
    with factory() as session, session.begin():
        session.add(
            FactRecord(
                id=fact_id,
                tenant_id="tenant-1",
                connection_id=None,
                provider="polar",
                entity_type="workout_session",
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
                confidence=0.9,
                freshness_at=NOW_S,
                raw_revision_id=None,
                raw_payload_id=None,
                schema_version="v1",
                normalizer_version="polar_workout_v0.1.0",
                content_hash=fact_id,
                fact_key=f"workout_session:{fact_id}",
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
            WorkoutSession(
                fact_record_id=fact_id,
                tenant_id="tenant-1",
                session_load=session_load,
                zone1_min=session_load,
                zone2_min=0.0,
                zone3_min=0.0,
                zone4_min=0.0,
                zone5_min=0.0,
            )
        )


def test_strain_load_query_sums_included_sessions(
    factory: sessionmaker[Session],
) -> None:
    _seed_workout(factory, day=TARGET_DAY, fact_id="w1", session_load=100.0)
    _seed_workout(factory, day=TARGET_DAY, fact_id="w2", session_load=50.0)
    loads = FactRepository(factory).daily_strain_load(
        tenant_id="tenant-1", local_health_days=[TARGET_DAY]
    )
    assert loads[TARGET_DAY] == pytest.approx(150.0)


def test_prior_load_activates_with_full_workout_coverage(
    factory: sessionmaker[Session],
) -> None:
    # Every day of the 28-day chronic window has a workout -> full coverage ->
    # ACWR defined -> the prior-load component is present.
    for offset in range(28):
        day = (datetime.fromisoformat(TARGET_DAY) - timedelta(days=offset)).date().isoformat()
        _seed_workout(factory, day=day, fact_id=f"w-{offset}", session_load=100.0)

    components = RecoveryInputService(features=FactRepository(factory)).recovery_components(
        tenant_id="tenant-1", local_health_day=TARGET_DAY
    )
    by_code = {c.code: c for c in components}
    assert ComponentCode.PRIOR_LOAD_BALANCE in by_code
    # Acute 700, chronic weekly 700 -> ACWR 1.0 -> balance band -> c = 100.
    assert by_code[ComponentCode.PRIOR_LOAD_BALANCE].c == pytest.approx(100.0)


def test_prior_load_omitted_with_a_coverage_gap(
    factory: sessionmaker[Session],
) -> None:
    # A single missing day in the 28-day window -> strict coverage fails ->
    # ACWR undefined -> component omitted.
    for offset in range(28):
        if offset == 5:
            continue  # leave day D-5 with no workout coverage
        day = (datetime.fromisoformat(TARGET_DAY) - timedelta(days=offset)).date().isoformat()
        _seed_workout(factory, day=day, fact_id=f"w-{offset}", session_load=100.0)

    components = RecoveryInputService(features=FactRepository(factory)).recovery_components(
        tenant_id="tenant-1", local_health_day=TARGET_DAY
    )
    assert ComponentCode.PRIOR_LOAD_BALANCE not in {c.code for c in components}


def test_acwr_for_day_matches_full_coverage(factory: sessionmaker[Session]) -> None:
    # Even load across the whole window -> acute 700, chronic weekly 700 ->
    # ACWR exactly 1.0. This is the value the training-label load rules read.
    for offset in range(28):
        day = (datetime.fromisoformat(TARGET_DAY) - timedelta(days=offset)).date().isoformat()
        _seed_workout(factory, day=day, fact_id=f"a-{offset}", session_load=100.0)

    acwr = RecoveryInputService(features=FactRepository(factory)).acwr_for_day(
        tenant_id="tenant-1", local_health_day=TARGET_DAY
    )
    assert acwr == pytest.approx(1.0)


def test_acwr_for_day_reflects_an_overload_spike(factory: sessionmaker[Session]) -> None:
    # A heavy last 7 days over a light chronic base pushes ACWR above the
    # balance band -> the training-label load downshift can fire.
    for offset in range(28):
        day = (datetime.fromisoformat(TARGET_DAY) - timedelta(days=offset)).date().isoformat()
        load = 200.0 if offset < 7 else 50.0
        _seed_workout(factory, day=day, fact_id=f"o-{offset}", session_load=load)

    acwr = RecoveryInputService(features=FactRepository(factory)).acwr_for_day(
        tenant_id="tenant-1", local_health_day=TARGET_DAY
    )
    assert acwr is not None
    # Acute = 7*200 = 1400; chronic = (7*200 + 21*50)/4 = 2450/4 = 612.5.
    assert acwr == pytest.approx(1400.0 / 612.5)
    assert acwr > 1.3  # past the descriptive balance band


def test_acwr_for_day_is_none_without_coverage(factory: sessionmaker[Session]) -> None:
    # One missing day -> undefined ACWR -> None, so load rules stay dormant.
    for offset in range(28):
        if offset == 10:
            continue
        day = (datetime.fromisoformat(TARGET_DAY) - timedelta(days=offset)).date().isoformat()
        _seed_workout(factory, day=day, fact_id=f"n-{offset}", session_load=100.0)

    acwr = RecoveryInputService(features=FactRepository(factory)).acwr_for_day(
        tenant_id="tenant-1", local_health_day=TARGET_DAY
    )
    assert acwr is None


class _EmptyFeatures:
    """A feature source with nothing known — the symptom path ignores it."""

    def daily_sleep_durations(self, *, tenant_id: str, local_health_days: list[str]):
        return {}

    def daily_sleep_efficiency(self, *, tenant_id: str, local_health_days: list[str]):
        return {}

    def daily_hrv(self, *, tenant_id: str, local_health_days: list[str]):
        return {}

    def daily_resting_hr(self, *, tenant_id: str, local_health_days: list[str]):
        return {}

    def daily_temperature_deviation(self, *, tenant_id: str, local_health_days: list[str]):
        return {}

    def daily_respiratory_rate(self, *, tenant_id: str, local_health_days: list[str]):
        return {}

    def daily_principal_sleep_midpoint(self, *, tenant_id: str, local_health_days: list[str]):
        return {}

    def daily_strain_load(self, *, tenant_id: str, local_health_days: list[str]):
        return {}


class _FakeSubjective:
    """A subjective source returning one fixed check-in (or absence)."""

    def __init__(self, inputs: SubjectiveInputs | None) -> None:
        self._inputs = inputs

    def current_check_in_inputs(
        self, *, tenant_id: str, local_health_day: str
    ) -> SubjectiveInputs | None:
        return self._inputs


def test_symptom_burden_for_day_reads_the_checkin() -> None:
    service = RecoveryInputService(
        features=_EmptyFeatures(),
        subjective=_FakeSubjective(
            SubjectiveInputs(energy_n=0.8, stress_n=0.2, symptom_burden_n=0.9)
        ),
    )
    assert service.symptom_burden_for_day(
        tenant_id="tenant-1", local_health_day=TARGET_DAY
    ) == pytest.approx(0.9)


def test_symptom_burden_for_day_is_none_without_a_checkin() -> None:
    service = RecoveryInputService(features=_EmptyFeatures(), subjective=_FakeSubjective(None))
    assert service.symptom_burden_for_day(tenant_id="tenant-1", local_health_day=TARGET_DAY) is None


def test_symptom_burden_for_day_is_none_without_a_source() -> None:
    # No subjective source wired at all -> None, never a fabricated zero.
    service = RecoveryInputService(features=_EmptyFeatures())
    assert service.symptom_burden_for_day(tenant_id="tenant-1", local_health_day=TARGET_DAY) is None
