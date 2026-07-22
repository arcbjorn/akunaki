"""The score.recompute job: assemble the surface and persist a score row.

Drives the handler through the **real worker runtime** against real
repositories, so the versioned write and the job's retry vocabulary are the
ones production uses. A day with HRV/RHR persists a real score; a sleep-only
day persists an honest ``insufficient`` row (an outcome worth storing, not an
absence).
"""

from __future__ import annotations

import itertools
from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.anomaly_repository import AnomalyRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.fact_repository import FactRepository
from akunaki.adapters.db.job_repository import JobRepository
from akunaki.adapters.db.models import (
    DailyHealthScore,
    FactRecord,
    OvernightVitals,
    ScoreFactor,
    SleepSession,
    Tenant,
)
from akunaki.adapters.db.score_repository import ScoreRepository
from akunaki.application.anomaly_tracker import AnomalyTracker
from akunaki.application.handlers import HandlerRegistry
from akunaki.application.recovery_inputs import RecoveryInputService
from akunaki.application.recovery_surface import RecoverySurfaceService
from akunaki.application.score_handlers import (
    SCORE_RECOMPUTE_JOB_TYPE,
    ScoreRecomputeHandler,
)
from akunaki.application.worker_runtime import JobWorker, WorkerConfig
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import JobStatus, to_utc_rfc3339

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
DAY = "2026-07-20"

_IDS = itertools.count(1)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "recompute.db"
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


def _registry(factory: sessionmaker[Session]) -> HandlerRegistry:
    facts = FactRepository(factory)
    inputs = RecoveryInputService(features=facts)
    handler = ScoreRecomputeHandler(
        recovery=RecoverySurfaceService(inputs=inputs),
        scores=ScoreRepository(factory),
        new_id=lambda: f"id-{next(_IDS)}",
        inputs=inputs,
        tracker=AnomalyTracker(
            store=AnomalyRepository(factory),
            new_id=lambda: f"an-{next(_IDS)}",
            clock=lambda: T0,
        ),
        clock=lambda: T0,
    )
    return HandlerRegistry({SCORE_RECOMPUTE_JOB_TYPE: handler})


def _run(factory: sessionmaker[Session], *, job_id: str = "rc-1", day: str = DAY) -> JobWorker:
    JobRepository(factory).enqueue_job(
        job_id=job_id,
        tenant_id="tenant-1",
        job_type=SCORE_RECOMPUTE_JOB_TYPE,
        payload_json=f'{{"local_health_day":"{day}"}}',
        now=T0,
    )
    worker = JobWorker(
        JobRepository(factory),
        owner="worker-1",
        config=WorkerConfig(lease_ttl=timedelta(seconds=60)),
        registry=_registry(factory),
        clock=lambda: T0,
        sleep=lambda _s: None,
        jitter=lambda: 0.0,
    )
    for _ in range(3):
        if not worker.run_once():
            break
    return worker


def _seed_sleep(factory: sessionmaker[Session], *, day: str, fact_id: str) -> None:
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
                tenant_id="tenant-1",
                is_nap=0,
                duration_min=470.0,
                time_in_bed_min=None,
                efficiency_pct=None,
                light_min=None,
                deep_min=None,
                rem_min=None,
                awake_min=None,
            )
        )


def _seed_vitals(
    factory: sessionmaker[Session],
    *,
    day: str,
    fact_id: str,
    hrv_ms: float = 60.0,
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
                resting_hr_bpm=50.0,
            )
        )


def _seed_full_history(factory: sessionmaker[Session]) -> None:
    """A mature vitals baseline plus today's sleep and vitals."""
    for offset in range(1, 29):
        day = (datetime.fromisoformat(DAY) - timedelta(days=offset)).date().isoformat()
        _seed_vitals(factory, day=day, fact_id=f"pv-{offset}")
    _seed_vitals(factory, day=DAY, fact_id="tv")
    _seed_sleep(factory, day=DAY, fact_id="ts")


# ---------------------------------------------------------------------------
# Persisting scores
# ---------------------------------------------------------------------------


def test_full_history_persists_a_real_score(factory: sessionmaker[Session]) -> None:
    _seed_full_history(factory)
    worker = _run(factory)

    assert worker.stats.succeeded == 1
    with factory() as session:
        row = session.scalars(select(DailyHealthScore)).one()
    assert row.score_code == "recovery"
    assert row.status != "insufficient"
    assert row.score is not None
    assert row.is_current == 1


def test_persisted_score_has_signed_factors(factory: sessionmaker[Session]) -> None:
    _seed_full_history(factory)
    _run(factory)

    with factory() as session:
        factors = {f.factor_code: f for f in session.scalars(select(ScoreFactor)).all()}
    assert factors["hrv"].present == 1
    assert factors["resting_hr"].present == 1
    # A component never supplied is disclosed as absent.
    assert factors["temperature"].present == 0


def test_sleep_only_day_persists_insufficient(factory: sessionmaker[Session]) -> None:
    _seed_sleep(factory, day=DAY, fact_id="ts")
    worker = _run(factory)

    assert worker.stats.succeeded == 1
    with factory() as session:
        row = session.scalars(select(DailyHealthScore)).one()
    assert row.status == "insufficient"
    assert row.score is None


def test_recompute_is_idempotent(factory: sessionmaker[Session]) -> None:
    _seed_full_history(factory)
    _run(factory, job_id="rc-1")
    _run(factory, job_id="rc-2")

    with factory() as session:
        rows = session.scalars(select(DailyHealthScore)).all()
    # Same inputs -> same dependency hash -> a single version.
    assert len(rows) == 1


def test_malformed_payload_dead_letters(factory: sessionmaker[Session]) -> None:
    JobRepository(factory).enqueue_job(
        job_id="rc-bad",
        tenant_id="tenant-1",
        job_type=SCORE_RECOMPUTE_JOB_TYPE,
        payload_json='{"wrong":"key"}',
        now=T0,
    )
    worker = JobWorker(
        JobRepository(factory),
        owner="worker-1",
        config=WorkerConfig(lease_ttl=timedelta(seconds=60)),
        registry=_registry(factory),
        clock=lambda: T0,
        sleep=lambda _s: None,
        jitter=lambda: 0.0,
    )
    for _ in range(3):
        if not worker.run_once():
            break

    assert worker.stats.dead_lettered == 1
    with factory() as session:
        from akunaki.adapters.db.models import Job

        job = session.get(Job, "rc-bad")
        assert job is not None
        assert job.status == JobStatus.DEAD_LETTER.value


def test_recompute_detects_and_persists_an_anomaly(
    factory: sessionmaker[Session],
) -> None:
    # A mature HRV baseline centered near 60 with small spread, then today's
    # HRV far below -> low-HRV anomaly opens during recompute.
    for offset in range(1, 29):
        day = (datetime.fromisoformat(DAY) - timedelta(days=offset)).date().isoformat()
        hrv = 58.0 if offset % 2 else 62.0  # spread so robust_scale > 0
        _seed_vitals(factory, day=day, fact_id=f"pv-{offset}", hrv_ms=hrv)
    _seed_vitals(factory, day=DAY, fact_id="tv", hrv_ms=20.0)
    _seed_sleep(factory, day=DAY, fact_id="ts")

    worker = _run(factory)
    assert worker.stats.succeeded == 1

    with factory() as session:
        from akunaki.adapters.db.models import Anomaly as AnomalyRow

        anomalies = session.scalars(select(AnomalyRow)).all()
    codes = {a.feature_code for a in anomalies}
    assert "low_hrv" in codes
    low_hrv = next(a for a in anomalies if a.feature_code == "low_hrv")
    assert low_hrv.is_active == 1
    assert low_hrv.started_on == DAY


def test_recompute_opens_no_anomaly_when_in_range(
    factory: sessionmaker[Session],
) -> None:
    for offset in range(1, 29):
        day = (datetime.fromisoformat(DAY) - timedelta(days=offset)).date().isoformat()
        hrv = 58.0 if offset % 2 else 62.0
        _seed_vitals(factory, day=day, fact_id=f"pv-{offset}", hrv_ms=hrv)
    _seed_vitals(factory, day=DAY, fact_id="tv", hrv_ms=61.0)  # normal
    _seed_sleep(factory, day=DAY, fact_id="ts")

    _run(factory)
    with factory() as session:
        from akunaki.adapters.db.models import Anomaly as AnomalyRow

        assert session.scalars(select(AnomalyRow)).all() == []
