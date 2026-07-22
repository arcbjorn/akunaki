"""Polar initial sync backfill through the provider-parameterized config.

The initial-sync handler is provider-agnostic. This drives it with the Polar
fetch client (over a mock transport) and the config from
``sync_config_for_provider("polar")``, proving the same handler backfills a
Polar connection: exercises are fetched, committed as raw revisions under the
``polar.v1`` schema, and normalized into ``workout_sessions``.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Generator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.connectors.polar_fetch import PolarFetchClient
from akunaki.adapters.crypto.envelope import KEY_BYTES, EnvelopeSealer
from akunaki.adapters.db.connection_repository import ConnectionRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.fact_repository import FactRepository
from akunaki.adapters.db.ingestion_repository import IngestionRepository, RevisionReader
from akunaki.adapters.db.job_repository import JobRepository
from akunaki.adapters.db.models import RawRevision, Tenant, WorkoutSession
from akunaki.application.handlers import HandlerRegistry
from akunaki.application.sync_handlers import (
    INITIAL_SYNC_JOB_TYPE,
    NORMALIZE_JOB_TYPE,
    InitialSyncHandler,
    NormalizeHandler,
    sync_config_for_provider,
)
from akunaki.application.worker_runtime import JobWorker, WorkerConfig
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.connections import Provider
from akunaki.domain.jobs import to_utc_rfc3339

T0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
KEK = b"\x66" * KEY_BYTES
ACCESS_TOKEN = "polar-access-SECRET"

# One AccessLink exercise with all five HR zones populated -> a real workout.
_EXERCISES = json.dumps(
    {
        "data": [
            {
                "id": "ex-1",
                "start_time": "2026-07-22T06:00:00+02:00",
                "duration": "PT1H",
                "heart_rate_zones": [
                    {"index": 1, "in_zone": "PT10M"},
                    {"index": 2, "in_zone": "PT20M"},
                    {"index": 3, "in_zone": "PT30M"},
                    {"index": 4, "in_zone": "PT5M"},
                    {"index": 5, "in_zone": "PT2M"},
                ],
            }
        ]
    }
)

_IDS = itertools.count(1)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def sync_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "polar_backfill.db"
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
def factory(sync_db: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=sync_db))
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
    # A linked Polar connection with sealed tokens, as the OAuth flow would
    # leave it once Polar token exchange is wired.
    sealer = EnvelopeSealer(keys={"v1": KEK}, active_key_version="v1")
    ConnectionRepository(session_factory).link(
        connection_id="conn-polar",
        tenant_id="tenant-1",
        provider=Provider.POLAR,
        sealed_secret=sealer.seal(
            json.dumps({"access_token": ACCESS_TOKEN, "refresh_token": "rt"}).encode(),
            aad=b"conn-polar",
        ),
        scopes=("accesslink.read_all",),
        external_user_id=None,
        now=T0,
    )
    try:
        yield session_factory
    finally:
        engine.dispose()


def _ok(body: str) -> Callable[[httpx2.Request], httpx2.Response]:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text=body, headers={"content-type": "application/json"})

    return handler


def _registry(factory: sessionmaker[Session]) -> HandlerRegistry:
    sealer = EnvelopeSealer(keys={"v1": KEK}, active_key_version="v1")
    new_id = lambda: f"id-{next(_IDS)}"  # noqa: E731
    initial = InitialSyncHandler(
        fetch_client=PolarFetchClient(
            transport=httpx2.Client(transport=httpx2.MockTransport(_ok(_EXERCISES)))
        ),
        ingestion=IngestionRepository(factory),
        connections=ConnectionRepository(factory),
        sealer=sealer,
        new_id=new_id,
        config=sync_config_for_provider("polar", max_pages=3),
        clock=lambda: T0,
    )
    normalize = NormalizeHandler(
        revisions=RevisionReader(factory),
        facts=FactRepository(factory),
        jobs=JobRepository(factory),
        new_id=new_id,
        clock=lambda: T0,
    )
    return HandlerRegistry(
        {
            INITIAL_SYNC_JOB_TYPE: initial,
            NORMALIZE_JOB_TYPE: normalize,
        }
    )


def _run(factory: sessionmaker[Session]) -> None:
    JobRepository(factory).enqueue_job(
        job_id="sync-1",
        tenant_id="tenant-1",
        job_type=INITIAL_SYNC_JOB_TYPE,
        payload_json='{"connection_id":"conn-polar"}',
        now=T0,
    )
    worker = JobWorker(
        JobRepository(factory),
        owner="worker-1",
        config=WorkerConfig(),
        registry=_registry(factory),
        clock=lambda: T0,
        sleep=lambda _s: None,
        jitter=lambda: 0.0,
    )
    # initial sync, then the chained normalize it enqueues.
    for _ in range(5):
        if not worker.run_once():
            break


def test_polar_backfill_writes_workout_facts(factory: sessionmaker[Session]) -> None:
    _run(factory)

    with factory() as session:
        revisions = session.scalars(select(RawRevision)).all()
        workouts = session.scalars(select(WorkoutSession)).all()

    # The exercise landed as a polar.v1 raw revision and normalized to a
    # workout with the expected zone minutes and canonical load.
    assert len(revisions) == 1
    assert revisions[0].schema_version == "polar.v1"
    assert len(workouts) == 1
    workout = workouts[0]
    assert workout.tenant_id == "tenant-1"
    assert workout.zone1_min == 10.0
    assert workout.zone5_min == 2.0
    # session_load = 10*1 + 20*2 + 30*3 + 5*4 + 2*5 = 170
    assert workout.session_load == pytest.approx(170.0)
