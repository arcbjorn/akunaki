"""Incremental sync handler: resume from the stored cursor, not a full lookback.

Wired against the real ingestion repository, connection repository, sealer, and
Oura fetch client (over a mock transport). Verifies the window start comes from
the last cursor (minus overlap) once one exists, falls back to the lookback on
the first run, and that the shared page loop still commits and advances.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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
from akunaki.adapters.db.ingestion_repository import IngestionRepository
from akunaki.adapters.db.job_repository import JobRepository
from akunaki.adapters.db.models import RawRevision, SyncCursor, Tenant
from akunaki.application.handlers import HandlerRegistry
from akunaki.application.sync_handlers import (
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_OVERLAP,
    INCREMENTAL_SYNC_JOB_TYPE,
    IncrementalSyncHandler,
    InitialSyncHandler,
    SyncConfig,
)
from akunaki.application.worker_runtime import JobWorker, WorkerConfig
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.connections import Provider
from akunaki.domain.jobs import to_utc_rfc3339

T0 = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
KEK = b"\x55" * KEY_BYTES
ACCESS_TOKEN = "oura-access-SECRET"

PAGE = json.dumps({"data": [{"id": "s1", "score": 82}], "next_token": None})

_ID_COUNTER = itertools.count(1)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def sync_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "incremental.db"
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
    sealer = EnvelopeSealer(keys={"v1": KEK}, active_key_version="v1")
    ConnectionRepository(session_factory).link(
        connection_id="conn-1",
        tenant_id="tenant-1",
        provider=Provider.OURA,
        sealed_secret=sealer.seal(
            json.dumps({"access_token": ACCESS_TOKEN, "refresh_token": "rt"}).encode(),
            aad=b"conn-1",
        ),
        scopes=("daily",),
        external_user_id=None,
        now=T0,
    )
    try:
        yield session_factory
    finally:
        engine.dispose()


def _capturing_responder(
    captured: list[str],
) -> Callable[[httpx2.Request], httpx2.Response]:
    def handler(request: httpx2.Request) -> httpx2.Response:
        captured.append(str(request.url))
        return httpx2.Response(200, text=PAGE, headers={"content-type": "application/json"})

    return handler


def _handler(
    factory: sessionmaker[Session],
    responder: Callable[[httpx2.Request], httpx2.Response],
) -> IncrementalSyncHandler:
    ids = (f"id-{n}" for n in _ID_COUNTER)
    ingestion = IngestionRepository(factory)
    backfill = InitialSyncHandler(
        fetch_client=OuraFetchClient(
            transport=httpx2.Client(transport=httpx2.MockTransport(responder))
        ),
        ingestion=ingestion,
        connections=ConnectionRepository(factory),
        sealer=EnvelopeSealer(keys={"v1": KEK}, active_key_version="v1"),
        new_id=lambda: next(ids),
        config=SyncConfig(max_pages=5),
        clock=lambda: T0,
    )
    return IncrementalSyncHandler(
        backfill=backfill,
        cursors=ingestion,
        config=SyncConfig(max_pages=5),
        clock=lambda: T0,
    )


def _run(factory: sessionmaker[Session], handler: IncrementalSyncHandler) -> None:
    repository = JobRepository(factory)
    repository.enqueue_job(
        job_id=f"job-{next(_ID_COUNTER)}",
        tenant_id="tenant-1",
        job_type=INCREMENTAL_SYNC_JOB_TYPE,
        payload_json='{"connection_id":"conn-1"}',
        now=T0,
        max_attempts=3,
    )
    worker = JobWorker(
        repository,
        owner="worker-1",
        config=WorkerConfig(lease_ttl=timedelta(seconds=60)),
        registry=HandlerRegistry({INCREMENTAL_SYNC_JOB_TYPE: handler}),
        clock=lambda: T0,
        sleep=lambda _s: None,
        jitter=lambda: 0.0,
    )
    worker.run_once()


def _start_date(url: str) -> str:
    return parse_qs(urlparse(url).query)["start_date"][0]


def test_first_run_without_cursor_uses_lookback(factory: sessionmaker[Session]) -> None:
    captured: list[str] = []
    _run(factory, _handler(factory, _capturing_responder(captured)))

    expected = (T0 - timedelta(days=DEFAULT_LOOKBACK_DAYS) - DEFAULT_OVERLAP).date().isoformat()
    assert _start_date(captured[0]) == expected
    # The page still commits a revision.
    with factory() as session:
        assert len(session.scalars(select(RawRevision)).all()) == 1


def test_resumes_from_the_stored_cursor(factory: sessionmaker[Session]) -> None:
    # A prior sync left a cursor 2 days ago; the next window resumes there
    # (minus overlap), not from the full lookback horizon.
    cursor_at = T0 - timedelta(days=2)
    with factory() as session, session.begin():
        session.add(
            SyncCursor(
                id="conn-1:sleep",
                tenant_id="tenant-1",
                connection_id="conn-1",
                cursor_type="timestamp",
                stream="sleep",
                cursor_value=to_utc_rfc3339(cursor_at),
                updated_at=NOW_S,
            )
        )

    captured: list[str] = []
    _run(factory, _handler(factory, _capturing_responder(captured)))

    expected = (cursor_at - DEFAULT_OVERLAP).date().isoformat()
    assert _start_date(captured[0]) == expected


def test_malformed_cursor_falls_back_to_lookback(factory: sessionmaker[Session]) -> None:
    with factory() as session, session.begin():
        session.add(
            SyncCursor(
                id="conn-1:sleep",
                tenant_id="tenant-1",
                connection_id="conn-1",
                cursor_type="timestamp",
                stream="sleep",
                cursor_value="not-a-timestamp",
                updated_at=NOW_S,
            )
        )

    captured: list[str] = []
    _run(factory, _handler(factory, _capturing_responder(captured)))

    # A bad cursor does not wedge the connection: it syncs the lookback window.
    expected = (T0 - timedelta(days=DEFAULT_LOOKBACK_DAYS) - DEFAULT_OVERLAP).date().isoformat()
    assert _start_date(captured[0]) == expected


def test_stale_cursor_is_clamped_to_lookback(factory: sessionmaker[Session]) -> None:
    # A cursor older than the lookback horizon must not widen the window beyond
    # it (guards an abandoned connection re-activated much later).
    cursor_at = T0 - timedelta(days=365)
    with factory() as session, session.begin():
        session.add(
            SyncCursor(
                id="conn-1:sleep",
                tenant_id="tenant-1",
                connection_id="conn-1",
                cursor_type="timestamp",
                stream="sleep",
                cursor_value=to_utc_rfc3339(cursor_at),
                updated_at=NOW_S,
            )
        )

    captured: list[str] = []
    _run(factory, _handler(factory, _capturing_responder(captured)))

    expected = (T0 - timedelta(days=DEFAULT_LOOKBACK_DAYS) - DEFAULT_OVERLAP).date().isoformat()
    assert _start_date(captured[0]) == expected
