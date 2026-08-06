"""Initial sync handler: fetch, atomic commit, and retry-vocabulary mapping.

Wired against the real ingestion repository, connection repository, sealer, and
Oura fetch client (over a mock transport), then driven through the **real
worker runtime** so the retry decisions are the ones the worker actually makes.
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
from akunaki.adapters.db.models import Connection, RawPayload, RawRevision, SyncCursor, Tenant
from akunaki.adapters.db.sync_run_repository import SyncRunRecord, SyncRunRepository
from akunaki.application.handlers import HandlerRegistry
from akunaki.application.sync_handlers import (
    INITIAL_SYNC_JOB_TYPE,
    NORMALIZE_JOB_TYPE,
    InitialSyncHandler,
    SyncConfig,
)
from akunaki.application.worker_runtime import JobWorker, WorkerConfig
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.connections import ConnectionStatus, Provider
from akunaki.domain.jobs import JobStatus, to_utc_rfc3339
from akunaki.domain.secrets import SealedSecret

T0 = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
KEK = b"\x55" * KEY_BYTES
ACCESS_TOKEN = "oura-access-SECRET"

PAGE_ONE = json.dumps({"data": [{"id": "s1", "score": 82}], "next_token": None})
PAGE_TWO = json.dumps({"data": [{"id": "s2", "score": 77}], "next_token": None})


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def sync_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "handler.db"
    url = f"sqlite+libsql:///{db_path.resolve()}"
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", url)
    clear_settings_cache()
    cfg = Config(str(_backend_root() / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(_backend_root() / "src" / "akunaki" / "migrations"))
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
    # A linked connection with sealed tokens, as the OAuth flow would leave it.
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


# One counter for the whole module: production supplies a UUID generator, so
# ids must stay unique across handler instances, not restart per handler.
_ID_COUNTER = itertools.count(1)


def _handler(
    factory: sessionmaker[Session],
    responder: Callable[[httpx2.Request], httpx2.Response],
    *,
    config: SyncConfig | None = None,
) -> InitialSyncHandler:
    ids = (f"id-{n}" for n in _ID_COUNTER)
    return InitialSyncHandler(
        fetch_client=OuraFetchClient(
            transport=httpx2.Client(transport=httpx2.MockTransport(responder))
        ),
        ingestion=IngestionRepository(factory),
        connections=ConnectionRepository(factory),
        sealer=EnvelopeSealer(keys={"v1": KEK}, active_key_version="v1"),
        new_id=lambda: next(ids),
        config=config or SyncConfig(max_pages=5),
        clock=lambda: T0,
    )


def _ok(body: str) -> Callable[[httpx2.Request], httpx2.Response]:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text=body, headers={"content-type": "application/json"})

    return handler


def _run_job(
    factory: sessionmaker[Session],
    handler: InitialSyncHandler,
    *,
    payload: str = '{"connection_id":"conn-1"}',
) -> JobWorker:
    """Enqueue and execute one job through the real worker runtime."""
    repository = JobRepository(factory)
    repository.enqueue_job(
        job_id="job-1",
        tenant_id="tenant-1",
        job_type=INITIAL_SYNC_JOB_TYPE,
        payload_json=payload,
        now=T0,
        max_attempts=3,
    )
    worker = JobWorker(
        repository,
        owner="worker-1",
        config=WorkerConfig(lease_ttl=timedelta(seconds=60)),
        registry=HandlerRegistry({INITIAL_SYNC_JOB_TYPE: handler}),
        clock=lambda: T0,
        sleep=lambda _s: None,
        jitter=lambda: 0.0,
    )
    worker.run_once()
    return worker


def _job_status(factory: sessionmaker[Session]) -> str:
    with factory() as session:
        from akunaki.adapters.db.models import Job

        job = session.get(Job, "job-1")
        assert job is not None
        return job.status


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_sync_persists_transport_revision_and_cursor(
    factory: sessionmaker[Session],
) -> None:
    worker = _run_job(factory, _handler(factory, _ok(PAGE_ONE)))

    assert worker.stats.succeeded == 1
    assert _job_status(factory) == JobStatus.SUCCEEDED.value

    with factory() as session:
        payloads = session.scalars(select(RawPayload)).all()
        revisions = session.scalars(select(RawRevision)).all()
        cursor = session.scalars(select(SyncCursor)).one()

    assert len(payloads) == 1
    assert payloads[0].payload_json == PAGE_ONE
    assert payloads[0].transport_kind == "sync_fetch"
    assert len(revisions) == 1
    assert revisions[0].revision_n == 1
    assert cursor.stream == "sleep"


def test_min_window_widens_a_short_initial_lookback(factory: sessionmaker[Session]) -> None:
    """A backfill narrower than the vendor floor is widened, not sent as-is.

    `lookback_days` is a public knob on `sync_config_for_provider`, so a caller
    can ask for a 3-day backfill — under Google Health's 14-day minimum, which
    the vendor rejects outright. The floor is enforced in the shared page loop
    so the initial path is covered too, not only the incremental resume.
    """
    captured: list[str] = []

    def responder(request: httpx2.Request) -> httpx2.Response:
        captured.append(str(request.url))
        return httpx2.Response(200, text=PAGE_ONE, headers={"content-type": "application/json"})

    _run_job(
        factory,
        _handler(
            factory,
            responder,
            config=SyncConfig(max_pages=5, lookback_days=3, min_window=timedelta(days=14)),
        ),
    )

    start_date = parse_qs(urlparse(captured[0]).query)["start_date"][0]
    # 14 days back from `now`, not the 3-day lookback the caller asked for.
    assert start_date == (T0 - timedelta(days=14)).date().isoformat()


def test_empty_backfill_warns_about_scopes(
    factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    """A full-lookback backfill that yields nothing must not look healthy.

    Oura answers a scope-deficient token with an empty array rather than an
    error, so an under-scoped grant is indistinguishable from an empty account
    at the transport layer. The job still succeeds (a brand-new user genuinely
    has no data), but it says so loudly enough to diagnose.
    """
    empty = json.dumps({"data": [], "next_token": None})
    with caplog.at_level("WARNING"):
        worker = _run_job(factory, _handler(factory, _ok(empty)))

    # Not an error: an empty account is legitimate.
    assert worker.stats.succeeded == 1
    assert _job_status(factory) == JobStatus.SUCCEEDED.value

    records = [r for r in caplog.records if "no records over the full lookback" in r.message]
    assert len(records) == 1
    assert records[0].hint == "verify the granted OAuth scopes cover this stream"  # type: ignore[attr-defined]
    assert records[0].provider == "oura"  # type: ignore[attr-defined]


def test_non_empty_backfill_does_not_warn(
    factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    """The scope warning must not fire on a healthy sync."""
    with caplog.at_level("WARNING"):
        _run_job(factory, _handler(factory, _ok(PAGE_ONE)))

    assert not [r for r in caplog.records if "no records over the full lookback" in r.message]


def test_request_metadata_carries_no_token(factory: sessionmaker[Session]) -> None:
    """Persisted transport metadata must never contain credentials."""
    _run_job(factory, _handler(factory, _ok(PAGE_ONE)))

    with factory() as session:
        payload = session.scalars(select(RawPayload)).one()

    assert ACCESS_TOKEN not in payload.request_meta_json
    meta = json.loads(payload.request_meta_json)
    assert meta["url_template"] == "v2/usercollection/sleep"


def test_access_token_is_sent_as_bearer(factory: sessionmaker[Session]) -> None:
    seen: list[httpx2.Request] = []

    def recording(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, text=PAGE_ONE, headers={"content-type": "application/json"})

    _run_job(factory, _handler(factory, recording))

    assert seen[0].headers["authorization"] == f"Bearer {ACCESS_TOKEN}"


def test_rerunning_the_same_sync_is_idempotent(factory: sessionmaker[Session]) -> None:
    """A retried job must not duplicate logical revisions."""
    _run_job(factory, _handler(factory, _ok(PAGE_ONE)))

    # Second execution of the same window, as a lease-expiry retry would do.
    handler = _handler(factory, _ok(PAGE_ONE))
    repository = JobRepository(factory)
    repository.enqueue_job(
        job_id="job-2",
        tenant_id="tenant-1",
        job_type=INITIAL_SYNC_JOB_TYPE,
        payload_json='{"connection_id":"conn-1"}',
        now=T0,
    )
    # The first sync also enqueued a raw.normalize job, so drain until the
    # second sync job has actually run.
    worker = JobWorker(
        repository,
        owner="worker-2",
        config=WorkerConfig(lease_ttl=timedelta(seconds=60)),
        registry=HandlerRegistry(
            {INITIAL_SYNC_JOB_TYPE: handler, NORMALIZE_JOB_TYPE: lambda _claim: None}
        ),
        clock=lambda: T0,
        sleep=lambda _s: None,
        jitter=lambda: 0.0,
    )
    for _ in range(4):
        if not worker.run_once():
            break

    with factory() as session:
        payloads = session.scalars(select(RawPayload)).all()
        revisions = session.scalars(select(RawRevision)).all()

    # Every response retained...
    assert len(payloads) == 2
    # ...but the logical revision is deduped by content hash.
    assert len(revisions) == 1


def test_pagination_follows_next_token(factory: sessionmaker[Session]) -> None:
    bodies = iter(
        [
            json.dumps({"data": [{"id": "s1"}], "next_token": "page-2"}),
            json.dumps({"data": [{"id": "s2"}], "next_token": None}),
        ]
    )

    def paged(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text=next(bodies), headers={"content-type": "application/json"})

    _run_job(factory, _handler(factory, paged))

    with factory() as session:
        assert len(session.scalars(select(RawPayload)).all()) == 2
        assert len(session.scalars(select(RawRevision)).all()) == 2


def test_page_cap_bounds_a_runaway_pagination(factory: sessionmaker[Session]) -> None:
    counter = itertools.count()

    def endless(_request: httpx2.Request) -> httpx2.Response:
        n = next(counter)
        return httpx2.Response(
            200,
            text=json.dumps({"data": [{"id": f"s{n}"}], "next_token": f"p{n}"}),
            headers={"content-type": "application/json"},
        )

    _run_job(factory, _handler(factory, endless, config=SyncConfig(max_pages=3)))

    with factory() as session:
        assert len(session.scalars(select(RawPayload)).all()) == 3


# ---------------------------------------------------------------------------
# Failure vocabulary
# ---------------------------------------------------------------------------


def test_unauthorized_flips_connection_and_does_not_retry(
    factory: sessionmaker[Session],
) -> None:
    """A dead grant must drive needs_reauth, never burn the attempt budget."""

    def unauthorized(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, json={"error": "unauthorized"})

    worker = _run_job(factory, _handler(factory, unauthorized))

    assert worker.stats.dead_lettered == 1
    assert worker.stats.retried == 0
    assert _job_status(factory) == JobStatus.DEAD_LETTER.value

    with factory() as session:
        connection = session.get(Connection, "conn-1")
        assert connection is not None
        assert connection.status == ConnectionStatus.NEEDS_REAUTH.value


def test_rate_limit_is_retried(factory: sessionmaker[Session]) -> None:
    def limited(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, json={"error": "rate_limit"}, headers={"retry-after": "120"})

    worker = _run_job(factory, _handler(factory, limited))

    assert worker.stats.retried == 1
    assert worker.stats.dead_lettered == 0
    assert _job_status(factory) == JobStatus.READY.value

    with factory() as session:
        connection = session.get(Connection, "conn-1")
        assert connection is not None
        # Transient failure must not demand re-authorization.
        assert connection.status == ConnectionStatus.ERROR.value


def test_server_error_is_retried(factory: sessionmaker[Session]) -> None:
    def broken(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503, json={"error": "unavailable"})

    worker = _run_job(factory, _handler(factory, broken))

    assert worker.stats.retried == 1
    assert _job_status(factory) == JobStatus.READY.value


def test_transport_error_is_retried(factory: sessionmaker[Session]) -> None:
    def boom(_request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    worker = _run_job(factory, _handler(factory, boom))

    assert worker.stats.retried == 1


def test_no_partial_write_when_fetch_fails(factory: sessionmaker[Session]) -> None:
    def broken(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503, json={"error": "unavailable"})

    _run_job(factory, _handler(factory, broken))

    with factory() as session:
        assert session.scalars(select(RawPayload)).all() == []
        assert session.scalars(select(RawRevision)).all() == []
        # The cursor must not advance past data that was never fetched.
        assert session.scalars(select(SyncCursor)).all() == []


def test_malformed_body_is_retried_not_stored(factory: sessionmaker[Session]) -> None:
    def html(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text="<html>gateway</html>")

    worker = _run_job(factory, _handler(factory, html))

    assert worker.stats.retried == 1
    with factory() as session:
        assert session.scalars(select(RawPayload)).all() == []


# ---------------------------------------------------------------------------
# Payload and credential validation
# ---------------------------------------------------------------------------


def test_malformed_payload_dead_letters(factory: sessionmaker[Session]) -> None:
    worker = _run_job(factory, _handler(factory, _ok(PAGE_ONE)), payload='{"nope":1}')

    assert worker.stats.dead_lettered == 1
    assert _job_status(factory) == JobStatus.DEAD_LETTER.value


def test_unopenable_credentials_dead_letter(factory: sessionmaker[Session]) -> None:
    """A KEK gap will not fix itself by retrying."""
    handler = InitialSyncHandler(
        fetch_client=OuraFetchClient(
            transport=httpx2.Client(transport=httpx2.MockTransport(_ok(PAGE_ONE)))
        ),
        ingestion=IngestionRepository(factory),
        connections=ConnectionRepository(factory),
        # Wrong KEK: the stored envelope cannot be opened.
        sealer=EnvelopeSealer(keys={"v1": b"\x99" * KEY_BYTES}, active_key_version="v1"),
        new_id=lambda: f"id-{next(_ID_COUNTER)}",
        config=SyncConfig(),
        clock=lambda: T0,
    )
    worker = _run_job(factory, handler)

    assert worker.stats.dead_lettered == 1


def test_missing_credentials_dead_letter(factory: sessionmaker[Session]) -> None:
    class NoSecret(ConnectionRepository):
        def get_sealed_secret(self, *, connection_id: str) -> SealedSecret | None:
            return None

    handler = InitialSyncHandler(
        fetch_client=OuraFetchClient(
            transport=httpx2.Client(transport=httpx2.MockTransport(_ok(PAGE_ONE)))
        ),
        ingestion=IngestionRepository(factory),
        connections=NoSecret(factory),
        sealer=EnvelopeSealer(keys={"v1": KEK}, active_key_version="v1"),
        new_id=lambda: f"id-{next(_ID_COUNTER)}",
        clock=lambda: T0,
    )
    worker = _run_job(factory, handler)

    assert worker.stats.dead_lettered == 1


def test_sync_config_validates() -> None:
    with pytest.raises(ValueError, match="lookback_days must be >= 1"):
        SyncConfig(lookback_days=0)
    with pytest.raises(ValueError, match="max_pages must be >= 1"):
        SyncConfig(max_pages=0)


# ---------------------------------------------------------------------------
# Sync run recording
# ---------------------------------------------------------------------------


def _recording_handler(
    factory: sessionmaker[Session],
    responder: Callable[[httpx2.Request], httpx2.Response],
) -> InitialSyncHandler:
    """The standard handler, plus the durable run recorder the worker wires."""
    ids = (f"id-{n}" for n in _ID_COUNTER)
    return InitialSyncHandler(
        fetch_client=OuraFetchClient(
            transport=httpx2.Client(transport=httpx2.MockTransport(responder))
        ),
        ingestion=IngestionRepository(factory),
        connections=ConnectionRepository(factory),
        sealer=EnvelopeSealer(keys={"v1": KEK}, active_key_version="v1"),
        new_id=lambda: next(ids),
        config=SyncConfig(max_pages=5),
        clock=lambda: T0,
        sync_runs=SyncRunRepository(factory),
    )


def _runs(factory: sessionmaker[Session]) -> list[SyncRunRecord]:
    return SyncRunRepository(factory).recent_for_tenant(tenant_id="tenant-1", limit=10)


def _rate_limited() -> Callable[[httpx2.Request], httpx2.Response]:
    def limited(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, json={"error": "rate_limit"}, headers={"retry-after": "120"})

    return limited


def _unauthorized() -> Callable[[httpx2.Request], httpx2.Response]:
    def denied(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, json={"error": "unauthorized"})

    return denied


def test_a_successful_sync_is_recorded(factory: sessionmaker[Session]) -> None:
    """``sync_runs`` had no writer at all until this path existed."""
    _run_job(factory, _recording_handler(factory, _ok(PAGE_ONE)))

    [run] = _runs(factory)

    assert run.status == "succeeded"
    assert run.trigger == "initial"
    assert run.stream == "sleep"
    assert run.finished_at is not None
    assert run.error_class is None


def test_a_failed_sync_is_still_recorded(factory: sessionmaker[Session]) -> None:
    """The whole point: a failure that leaves no trace is the current bug.

    ``connection_health`` counts failures but cannot say when one happened or
    whether the last attempt even ran.
    """
    _run_job(factory, _recording_handler(factory, _rate_limited()))

    [run] = _runs(factory)

    assert run.status == "failed"
    assert run.finished_at is not None
    assert run.error_class == "TransientJobError"


def test_an_auth_failure_records_its_permanent_class(factory: sessionmaker[Session]) -> None:
    _run_job(factory, _recording_handler(factory, _unauthorized()))

    [run] = _runs(factory)

    assert run.status == "failed"
    assert run.error_class == "PermanentJobError"


def test_a_recorded_failure_carries_no_vendor_body(factory: sessionmaker[Session]) -> None:
    """The error *class*, never the provider's message."""
    _run_job(factory, _recording_handler(factory, _rate_limited()))

    [run] = _runs(factory)

    assert run.error_class is not None
    assert "rate_limit" not in run.error_class


def test_ingested_rows_link_back_to_their_run(factory: sessionmaker[Session]) -> None:
    """``raw_payloads.sync_run_id`` was permanently NULL before the recorder.

    That FK exists so ingested data is traceable to the fetch that produced it.
    """
    _run_job(factory, _recording_handler(factory, _ok(PAGE_ONE)))

    [run] = _runs(factory)
    with factory() as session:
        payloads = session.scalars(select(RawPayload)).all()

    assert payloads
    assert all(payload.sync_run_id == run.run_id for payload in payloads)


def test_a_handler_without_a_recorder_still_syncs(factory: sessionmaker[Session]) -> None:
    """Recording is additive: an unwired handler behaves exactly as before."""
    worker = _run_job(factory, _handler(factory, _ok(PAGE_ONE)))

    assert worker.stats.succeeded == 1
    assert _runs(factory) == []


class _BrokenRecorder:
    """A recorder whose close always fails, as a locked database would."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._real = SyncRunRepository(factory)

    def open(self, **kwargs: object) -> str:
        return self._real.open(**kwargs)  # type: ignore[arg-type]

    def close(self, **_kwargs: object) -> bool:
        msg = "database is locked"
        raise RuntimeError(msg)


def _handler_with_recorder(
    factory: sessionmaker[Session],
    responder: Callable[[httpx2.Request], httpx2.Response],
    recorder: object,
) -> InitialSyncHandler:
    ids = (f"id-{n}" for n in _ID_COUNTER)
    return InitialSyncHandler(
        fetch_client=OuraFetchClient(
            transport=httpx2.Client(transport=httpx2.MockTransport(responder))
        ),
        ingestion=IngestionRepository(factory),
        connections=ConnectionRepository(factory),
        sealer=EnvelopeSealer(keys={"v1": KEK}, active_key_version="v1"),
        new_id=lambda: next(ids),
        config=SyncConfig(max_pages=5),
        clock=lambda: T0,
        sync_runs=recorder,  # type: ignore[arg-type]
    )


def test_a_failed_close_does_not_fail_a_successful_sync(
    factory: sessionmaker[Session],
) -> None:
    """History is bookkeeping; it must never break the sync it describes.

    The close runs in a ``finally``, so a raised write would fail a sync whose
    data is already committed — and the runtime would retry work that worked.
    """
    handler = _handler_with_recorder(factory, _ok(PAGE_ONE), _BrokenRecorder(factory))

    worker = _run_job(factory, handler)

    assert worker.stats.succeeded == 1
    with factory() as session:
        assert session.scalars(select(RawPayload)).all()


def test_a_failed_close_does_not_mask_the_fetch_error(
    factory: sessionmaker[Session],
) -> None:
    """An exception from ``finally`` would *replace* the real failure.

    A vendor rate limit must still reach the runtime as a retryable error, not
    be swapped for a confusing database one.
    """
    handler = _handler_with_recorder(factory, _rate_limited(), _BrokenRecorder(factory))

    worker = _run_job(factory, handler)

    # Still classified as transient by the vendor 429, not by the close failure.
    assert worker.stats.retried == 1
    assert worker.stats.dead_lettered == 0


def test_a_successful_run_records_its_ingest_count(factory: sessionmaker[Session]) -> None:
    """``stats_json`` is 'counts only' per the schema, and was always NULL."""
    _run_job(factory, _recording_handler(factory, _ok(PAGE_ONE)))

    [run] = _runs(factory)

    assert run.new_revisions == 1


def test_a_failed_run_records_a_zero_count(factory: sessionmaker[Session]) -> None:
    """Zero is the honest count for a fetch that never committed anything."""
    _run_job(factory, _recording_handler(factory, _rate_limited()))

    [run] = _runs(factory)

    assert run.status == "failed"
    assert run.new_revisions == 0
