"""Closed ingestion loop: sync enqueues normalize, normalize writes facts.

Drives both handlers through the **real worker runtime** against the real
repositories, so the enqueue-on-commit guarantee and the handler's retry
vocabulary are the ones production uses.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.connectors.oura_fetch import OuraFetchClient
from akunaki.adapters.crypto.envelope import KEY_BYTES, EnvelopeSealer
from akunaki.adapters.db.connection_repository import ConnectionRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.fact_repository import FactRepository
from akunaki.adapters.db.ingestion_repository import IngestionRepository, RevisionReader
from akunaki.adapters.db.job_repository import JobRepository
from akunaki.adapters.db.models import (
    FactRecord,
    Job,
    RawRevision,
    SleepSession,
    Tenant,
)
from akunaki.application.handlers import HandlerRegistry
from akunaki.application.sync_handlers import (
    INITIAL_SYNC_JOB_TYPE,
    NORMALIZE_JOB_TYPE,
    InitialSyncHandler,
    NormalizeHandler,
    SyncConfig,
)
from akunaki.application.worker_runtime import JobWorker, WorkerConfig
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.connections import Provider
from akunaki.domain.jobs import JobStatus, to_utc_rfc3339

T0 = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
KEK = b"\x66" * KEY_BYTES

SLEEP_PAGE = json.dumps(
    {
        "data": [
            {
                "id": "sleep-abc",
                "bedtime_start": "2026-07-18T23:10:00+02:00",
                "bedtime_end": "2026-07-19T07:20:00+02:00",
                "total_sleep_duration": 27000,
                "time_in_bed": 29400,
                "light_sleep_duration": 15000,
                "deep_sleep_duration": 6000,
                "rem_sleep_duration": 6000,
                "awake_time": 2400,
                "efficiency": 92,
                "type": "long_sleep",
            }
        ],
        "next_token": None,
    }
)

_IDS = itertools.count(1)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def loop_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "loop.db"
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
def factory(loop_db: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=loop_db))
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
    sealer = EnvelopeSealer(keys={"v1": KEK}, active_key_version="v1")
    ConnectionRepository(session_factory).link(
        connection_id="conn-1",
        tenant_id="tenant-1",
        provider=Provider.OURA,
        sealed_secret=sealer.seal(json.dumps({"access_token": "AT"}).encode(), aad=b"conn-1"),
        scopes=("daily",),
        external_user_id=None,
        now=T0,
    )
    try:
        yield session_factory
    finally:
        engine.dispose()


def _registry(
    factory: sessionmaker[Session],
    responder: Callable[[httpx2.Request], httpx2.Response],
) -> HandlerRegistry:
    """Both handlers, wired to the same real repositories."""
    new_id = lambda: f"id-{next(_IDS)}"  # noqa: E731
    sync = InitialSyncHandler(
        fetch_client=OuraFetchClient(
            transport=httpx2.Client(transport=httpx2.MockTransport(responder))
        ),
        ingestion=IngestionRepository(factory),
        connections=ConnectionRepository(factory),
        sealer=EnvelopeSealer(keys={"v1": KEK}, active_key_version="v1"),
        new_id=new_id,
        config=SyncConfig(max_pages=3),
        clock=lambda: T0,
    )
    normalize = NormalizeHandler(
        revisions=RevisionReader(factory),
        facts=FactRepository(factory),
        new_id=new_id,
        clock=lambda: T0,
    )
    return HandlerRegistry({INITIAL_SYNC_JOB_TYPE: sync, NORMALIZE_JOB_TYPE: normalize})


def _drain(
    factory: sessionmaker[Session],
    registry: HandlerRegistry,
    *,
    max_iterations: int = 10,
) -> JobWorker:
    """Run the worker until the queue is empty."""
    worker = JobWorker(
        JobRepository(factory),
        owner="worker-1",
        config=WorkerConfig(lease_ttl=timedelta(seconds=60)),
        registry=registry,
        clock=lambda: T0,
        sleep=lambda _s: None,
        jitter=lambda: 0.0,
    )
    for _ in range(max_iterations):
        if not worker.run_once():
            break
    return worker


def _start_sync(factory: sessionmaker[Session], *, job_id: str = "sync-1") -> None:
    JobRepository(factory).enqueue_job(
        job_id=job_id,
        tenant_id="tenant-1",
        job_type=INITIAL_SYNC_JOB_TYPE,
        payload_json='{"connection_id":"conn-1"}',
        now=T0,
    )


def _ok(body: str) -> Callable[[httpx2.Request], httpx2.Response]:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text=body, headers={"content-type": "application/json"})

    return handler


# ---------------------------------------------------------------------------
# Closed loop
# ---------------------------------------------------------------------------


def test_sync_enqueues_normalize_and_facts_are_written(
    factory: sessionmaker[Session],
) -> None:
    _start_sync(factory)
    worker = _drain(factory, _registry(factory, _ok(SLEEP_PAGE)))

    # Sync job plus the normalize job it enqueued.
    assert worker.stats.succeeded == 2

    with factory() as session:
        facts = session.scalars(select(FactRecord)).all()
        detail = session.scalars(select(SleepSession)).all()

    assert len(facts) == 1
    assert facts[0].local_health_day == "2026-07-19"
    assert facts[0].entity_type == "sleep_session"
    assert facts[0].is_current == 1
    assert len(detail) == 1
    assert detail[0].duration_min == 450.0


def test_normalize_job_is_enqueued_in_the_same_commit(
    factory: sessionmaker[Session],
) -> None:
    """A revision must never exist without its normalization job."""
    _start_sync(factory)
    # Run only the sync job; do not drain the normalize job.
    JobWorker(
        JobRepository(factory),
        owner="worker-1",
        config=WorkerConfig(lease_ttl=timedelta(seconds=60)),
        registry=_registry(factory, _ok(SLEEP_PAGE)),
        clock=lambda: T0,
        sleep=lambda _s: None,
        jitter=lambda: 0.0,
    ).run_once()

    with factory() as session:
        revisions = session.scalars(select(RawRevision)).all()
        normalize_jobs = session.scalars(
            select(Job).where(Job.job_type == NORMALIZE_JOB_TYPE)
        ).all()

    assert len(revisions) == 1
    assert len(normalize_jobs) == 1
    payload = json.loads(normalize_jobs[0].payload_json)
    assert payload["raw_revision_id"] == revisions[0].id


def test_facts_carry_raw_lineage(factory: sessionmaker[Session]) -> None:
    _start_sync(factory)
    _drain(factory, _registry(factory, _ok(SLEEP_PAGE)))

    with factory() as session:
        fact = session.scalars(select(FactRecord)).one()
        revision = session.scalars(select(RawRevision)).one()

    assert fact.raw_revision_id == revision.id
    assert fact.raw_payload_id == revision.raw_payload_id
    assert fact.connection_id == "conn-1"


def test_repeated_sync_writes_no_duplicate_facts(
    factory: sessionmaker[Session],
) -> None:
    """Unchanged data: no new revision, no new normalize job, no new fact."""
    _start_sync(factory, job_id="sync-1")
    _drain(factory, _registry(factory, _ok(SLEEP_PAGE)))

    _start_sync(factory, job_id="sync-2")
    _drain(factory, _registry(factory, _ok(SLEEP_PAGE)))

    with factory() as session:
        revisions = session.scalars(select(RawRevision)).all()
        facts = session.scalars(select(FactRecord)).all()
        normalize_jobs = session.scalars(
            select(Job).where(Job.job_type == NORMALIZE_JOB_TYPE)
        ).all()

    assert len(revisions) == 1
    assert len(normalize_jobs) == 1
    assert len(facts) == 1


def test_corrected_night_supersedes_the_earlier_fact(
    factory: sessionmaker[Session],
) -> None:
    """A vendor correction flows all the way through to a new fact version."""
    _start_sync(factory, job_id="sync-1")
    _drain(factory, _registry(factory, _ok(SLEEP_PAGE)))

    corrected = SLEEP_PAGE.replace('"total_sleep_duration": 27000', '"total_sleep_duration": 28800')
    _start_sync(factory, job_id="sync-2")
    _drain(factory, _registry(factory, _ok(corrected)))

    with factory() as session:
        facts = session.scalars(select(FactRecord).order_by(FactRecord.version_n)).all()

    assert len(facts) == 2
    assert facts[0].is_current == 0
    assert facts[1].is_current == 1
    assert facts[1].version_n == 2


# ---------------------------------------------------------------------------
# Normalize handler failure modes
# ---------------------------------------------------------------------------


def _run_normalize(
    factory: sessionmaker[Session],
    payload: str,
) -> JobWorker:
    JobRepository(factory).enqueue_job(
        job_id="norm-1",
        tenant_id="tenant-1",
        job_type=NORMALIZE_JOB_TYPE,
        payload_json=payload,
        now=T0,
    )
    return _drain(factory, _registry(factory, _ok(SLEEP_PAGE)), max_iterations=2)


def test_missing_revision_dead_letters(factory: sessionmaker[Session]) -> None:
    """A stale job pointing at no revision will not fix itself by retrying."""
    worker = _run_normalize(factory, '{"raw_revision_id":"nope"}')

    assert worker.stats.dead_lettered == 1
    with factory() as session:
        job = session.get(Job, "norm-1")
        assert job is not None
        assert job.status == JobStatus.DEAD_LETTER.value


def test_malformed_normalize_payload_dead_letters(
    factory: sessionmaker[Session],
) -> None:
    worker = _run_normalize(factory, '{"wrong_key":1}')
    assert worker.stats.dead_lettered == 1


def test_unparseable_body_dead_letters(factory: sessionmaker[Session]) -> None:
    """A body that will never parse must not be retried forever."""
    _start_sync(factory)
    # Sync stores an HTML body only if it parses as JSON, so write a payload
    # whose JSON is valid but whose shape the normalizer rejects.
    _drain(factory, _registry(factory, _ok(json.dumps({"unexpected": True}))))

    with factory() as session:
        jobs = session.scalars(select(Job).where(Job.job_type == NORMALIZE_JOB_TYPE)).all()

    assert len(jobs) == 1
    assert jobs[0].status == JobStatus.DEAD_LETTER.value


def test_tombstone_revision_is_skipped_not_normalized(
    factory: sessionmaker[Session],
) -> None:
    """Vendor deletions go through the deletion path, not the normalizer."""
    _start_sync(factory)
    JobWorker(
        JobRepository(factory),
        owner="worker-1",
        config=WorkerConfig(lease_ttl=timedelta(seconds=60)),
        registry=_registry(factory, _ok(SLEEP_PAGE)),
        clock=lambda: T0,
        sleep=lambda _s: None,
        jitter=lambda: 0.0,
    ).run_once()

    with factory() as session, session.begin():
        revision = session.scalars(select(RawRevision)).one()
        revision.is_tombstone = 1
        revision.tombstone_reason = "vendor_deleted"
        revision.deletion_state = "vendor_deleted"

    worker = _drain(factory, _registry(factory, _ok(SLEEP_PAGE)))

    # The job succeeds (nothing to do), and no fact is fabricated.
    assert worker.stats.dead_lettered == 0
    with factory() as session:
        assert session.scalars(select(FactRecord)).all() == []
