"""Google Health initial-sync backfill through the parameterized config.

The initial-sync handler is provider-agnostic. This drives it with the Google
Health fetch client (over a mock transport) and the config from
``sync_config_for_provider("google_health")``, proving the same handler
backfills a Google Health connection: sleep segments are fetched, committed as a
raw revision under ``google_health.v4``, and normalized into ``sleep_sessions``.
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

from akunaki.adapters.connectors.google_health_fetch import GoogleHealthFetchClient
from akunaki.adapters.crypto.envelope import KEY_BYTES, EnvelopeSealer
from akunaki.adapters.db.connection_repository import ConnectionRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.fact_repository import FactRepository
from akunaki.adapters.db.ingestion_repository import IngestionRepository, RevisionReader
from akunaki.adapters.db.job_repository import JobRepository
from akunaki.adapters.db.models import RawRevision, SleepSession, Tenant
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
KEK = b"\x77" * KEY_BYTES
ACCESS_TOKEN = "google-access-SECRET"

# One night of sleep stages: 240 light + 180 deep = 420 sleep minutes.
_SLEEP_PAGE = json.dumps(
    {
        "dataPoints": [
            {
                "startTime": "2026-07-22T00:00:00+02:00",
                "endTime": "2026-07-22T04:00:00+02:00",
                "sleepType": "SLEEP_STAGE_LIGHT",
            },
            {
                "startTime": "2026-07-22T04:00:00+02:00",
                "endTime": "2026-07-22T07:00:00+02:00",
                "sleepType": "SLEEP_STAGE_DEEP",
            },
        ]
    }
)

_IDS = itertools.count(1)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def sync_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "google_backfill.db"
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
    # A linked Google Health connection with sealed tokens.
    sealer = EnvelopeSealer(keys={"v1": KEK}, active_key_version="v1")
    ConnectionRepository(session_factory).link(
        connection_id="conn-google",
        tenant_id="tenant-1",
        provider=Provider.GOOGLE_HEALTH,
        sealed_secret=sealer.seal(
            json.dumps({"access_token": ACCESS_TOKEN, "refresh_token": "rt"}).encode(),
            aad=b"conn-google",
        ),
        scopes=("https://www.googleapis.com/auth/health.sleep.read",),
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
        fetch_client=GoogleHealthFetchClient(
            transport=httpx2.Client(transport=httpx2.MockTransport(_ok(_SLEEP_PAGE)))
        ),
        ingestion=IngestionRepository(factory),
        connections=ConnectionRepository(factory),
        sealer=sealer,
        new_id=new_id,
        config=sync_config_for_provider("google_health", max_pages=3),
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
        payload_json='{"connection_id":"conn-google"}',
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


def test_google_backfill_writes_sleep_facts(factory: sessionmaker[Session]) -> None:
    _run(factory)

    with factory() as session:
        revisions = session.scalars(select(RawRevision)).all()
        sleeps = session.scalars(select(SleepSession)).all()

    # The page landed as a google_health.v4 raw revision and normalized to one
    # aggregated sleep session with the expected stage minutes.
    assert len(revisions) == 1
    assert revisions[0].schema_version == "google_health.v4"
    assert len(sleeps) == 1
    sleep = sleeps[0]
    assert sleep.tenant_id == "tenant-1"
    assert sleep.duration_min == pytest.approx(420.0)
    assert sleep.light_min == pytest.approx(240.0)
    assert sleep.deep_min == pytest.approx(180.0)
