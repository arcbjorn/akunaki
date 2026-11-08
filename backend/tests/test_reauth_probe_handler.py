"""The reauth probe: a parked connection is re-checked, not abandoned.

Until 2026-11-08 ``needs_reauth`` was terminal from the system's side. The
probe asks the provider whether the stored refresh token still works and acts
on the answer: a working refresh restores the connection and queues its syncs;
``invalid_grant`` leaves it parked and paced; anything else is no verdict.
Runs against the real repositories, sealer, and worker runtime, with a canned
OAuth client standing in for the provider's token endpoint.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.crypto.envelope import KEY_BYTES, EnvelopeSealer
from akunaki.adapters.db.connection_repository import ConnectionRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.job_repository import JobRepository
from akunaki.adapters.db.models import Connection, ConnectionHealth, Job, Tenant
from akunaki.application.handlers import HandlerRegistry
from akunaki.application.reauth_handlers import REAUTH_PROBE_JOB_TYPE, ReauthProbeHandler
from akunaki.application.sync_handlers import INCREMENTAL_SYNC_JOB_TYPE, streams_for_provider
from akunaki.application.worker_runtime import JobWorker, WorkerConfig
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.connections import ConnectionStatus, Provider
from akunaki.domain.jobs import to_utc_rfc3339
from akunaki.domain.tokens import (
    EnrollmentResult,
    OAuthTokens,
    TokenExchangeFailure,
    TokenExchangeResult,
)
from akunaki.ports.oauth_client import OAuthClientPort
from conftest import upgrade_to_head

T0 = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
PARKED_AT = T0 - timedelta(days=1)
KEK = b"\x55" * KEY_BYTES
_ID_COUNTER = itertools.count(1)


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "probe.db"
    url = f"sqlite+libsql:///{db_path.resolve()}"
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", url)
    clear_settings_cache()
    upgrade_to_head(url)
    yield url
    clear_settings_cache()


def _sealer() -> EnvelopeSealer:
    return EnvelopeSealer(keys={"v1": KEK}, active_key_version="v1")


def _reseal(factory: sessionmaker[Session], tokens: dict[str, object]) -> None:
    ConnectionRepository(factory).replace_sealed_secret(
        connection_id="conn-1",
        sealed_secret=_sealer().seal(json.dumps(tokens).encode(), aad=b"conn-1"),
        now=T0,
    )


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
    repo = ConnectionRepository(session_factory)
    repo.link(
        connection_id="conn-1",
        tenant_id="tenant-1",
        provider=Provider.GOOGLE_HEALTH,
        sealed_secret=_sealer().seal(
            json.dumps(
                {
                    "access_token": "stale-access",
                    "refresh_token": "the-refresh-token",
                    "expires_at": "2026-11-04T15:00:00Z",
                }
            ).encode(),
            aad=b"conn-1",
        ),
        scopes=(),
        external_user_id=None,
        now=T0 - timedelta(days=30),
    )
    # Parked a day ago, as the 2026-11-04 transport error left it.
    repo.mark_status(
        connection_id="conn-1",
        status=ConnectionStatus.NEEDS_REAUTH,
        now=PARKED_AT,
        error_class="unauthorized",
    )
    try:
        yield session_factory
    finally:
        engine.dispose()


class _StubOAuthClient:
    """Records refresh calls and returns a canned result."""

    def __init__(self, result: TokenExchangeResult) -> None:
        self._result = result
        self.refreshed_with: list[str] = []

    @property
    def provider(self) -> str:
        return "google_health"

    @property
    def uses_pkce(self) -> bool:
        return True

    @property
    def requires_enrollment(self) -> bool:
        return False

    def authorize_url(
        self,
        *,
        state: str,
        code_challenge: str | None,
        redirect_uri: str,
        scopes: tuple[str, ...],
    ) -> str:  # pragma: no cover - unused
        return ""

    def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str | None,
        redirect_uri: str,
        now: datetime,
    ) -> TokenExchangeResult:  # pragma: no cover - unused
        return self._result

    def enroll_user(
        self,
        *,
        access_token: str,
        external_user_id: str | None,
    ) -> EnrollmentResult:  # pragma: no cover - unused
        return EnrollmentResult()

    def refresh(self, *, refresh_token: str, now: datetime) -> TokenExchangeResult:
        self.refreshed_with.append(refresh_token)
        return self._result


def _fresh_tokens() -> TokenExchangeResult:
    return TokenExchangeResult(
        tokens=OAuthTokens(
            access_token="fresh-access",
            refresh_token=None,
            expires_at="2026-11-08T13:00:00Z",
            scopes=(),
            token_type="bearer",
        )
    )


def _handler(
    factory: sessionmaker[Session],
    oauth: OAuthClientPort,
    *,
    clients: dict[str, OAuthClientPort] | None = None,
) -> ReauthProbeHandler:
    return ReauthProbeHandler(
        connections=ConnectionRepository(factory),
        sealer=_sealer(),
        oauth_clients=clients if clients is not None else {"google_health": oauth},
        jobs=JobRepository(factory),
        new_id=lambda: f"id-{next(_ID_COUNTER)}",
        clock=lambda: T0,
    )


def _run(
    factory: sessionmaker[Session],
    handler: ReauthProbeHandler,
    *,
    payload: str = '{"connection_id":"conn-1"}',
) -> JobWorker:
    repository = JobRepository(factory)
    repository.enqueue_job(
        job_id=f"probe-{next(_ID_COUNTER)}",
        tenant_id="tenant-1",
        job_type=REAUTH_PROBE_JOB_TYPE,
        payload_json=payload,
        now=T0,
        max_attempts=3,
    )
    worker = JobWorker(
        repository,
        owner="worker-1",
        config=WorkerConfig(lease_ttl=timedelta(seconds=60)),
        registry=HandlerRegistry({REAUTH_PROBE_JOB_TYPE: handler}),
        clock=lambda: T0,
        sleep=lambda _s: None,
        jitter=lambda: 0.0,
    )
    worker.run_once()
    return worker


def _connection(factory: sessionmaker[Session]) -> tuple[str, str, str | None]:
    """(status, updated_at, last_error_class)."""
    with factory() as session:
        row = session.get(Connection, "conn-1")
        health = session.get(ConnectionHealth, "conn-1")
    assert row is not None
    return row.status, row.updated_at, health.last_error_class if health is not None else None


def _sync_jobs(factory: sessionmaker[Session]) -> list[Job]:
    with factory() as session:
        return list(
            session.scalars(select(Job).where(Job.job_type == INCREMENTAL_SYNC_JOB_TYPE)).all()
        )


def _stored_tokens(factory: sessionmaker[Session]) -> dict[str, object]:
    sealed = ConnectionRepository(factory).get_sealed_secret(connection_id="conn-1")
    assert sealed is not None
    return dict(json.loads(_sealer().open(sealed, aad=b"conn-1")))


def test_a_working_refresh_restores_the_connection_and_queues_its_syncs(
    factory: sessionmaker[Session],
) -> None:
    """The grant was alive all along; the connection comes back without the user.

    The new tokens are persisted (keeping the refresh token Google omits on
    refresh), the status returns to ``active``, and one incremental sync per
    stream is queued under the sweep's own keys so nothing is scheduled twice.
    """
    oauth = _StubOAuthClient(_fresh_tokens())

    worker = _run(factory, _handler(factory, oauth))

    assert worker.stats.succeeded == 1
    assert oauth.refreshed_with == ["the-refresh-token"]
    status, _updated_at, error_class = _connection(factory)
    assert status == ConnectionStatus.ACTIVE.value
    assert error_class is None
    stored = _stored_tokens(factory)
    assert stored["access_token"] == "fresh-access"
    assert stored["refresh_token"] == "the-refresh-token"
    jobs = _sync_jobs(factory)
    expected = {stream for stream, _ in streams_for_provider("google_health")}
    assert {json.loads(j.payload_json)["stream"] for j in jobs} == expected
    assert {j.idempotency_key for j in jobs} == {f"reconcile:conn-1:{s}" for s in expected}


def test_a_refused_grant_stays_parked_and_is_paced(factory: sessionmaker[Session]) -> None:
    """``invalid_grant`` is a state, not a failure: no dead letter, and the
    re-stamped ``updated_at`` makes the next probe wait a full interval."""
    oauth = _StubOAuthClient(TokenExchangeResult(failure=TokenExchangeFailure.INVALID_GRANT))

    worker = _run(factory, _handler(factory, oauth))

    assert worker.stats.succeeded == 1
    assert worker.stats.dead_lettered == 0
    assert _connection(factory) == (ConnectionStatus.NEEDS_REAUTH.value, NOW_S, "invalid_grant")
    assert _sync_jobs(factory) == []
    assert _stored_tokens(factory)["access_token"] == "stale-access"


def test_no_verdict_is_paced_like_a_refusal(factory: sessionmaker[Session]) -> None:
    oauth = _StubOAuthClient(TokenExchangeResult(failure=TokenExchangeFailure.TRANSPORT_ERROR))

    worker = _run(factory, _handler(factory, oauth))

    assert worker.stats.succeeded == 1
    assert _connection(factory) == (ConnectionStatus.NEEDS_REAUTH.value, NOW_S, "transport_error")


def test_a_raising_refresh_is_paced_too(factory: sessionmaker[Session]) -> None:
    class _Exploding(_StubOAuthClient):
        def refresh(self, *, refresh_token: str, now: datetime) -> TokenExchangeResult:
            raise RuntimeError("transport exploded")

    worker = _run(factory, _handler(factory, _Exploding(_fresh_tokens())))

    assert worker.stats.succeeded == 1
    assert _connection(factory) == (ConnectionStatus.NEEDS_REAUTH.value, NOW_S, "probe_failed")


def test_a_connection_no_longer_parked_is_left_alone(factory: sessionmaker[Session]) -> None:
    """A re-link or an earlier probe got there first: do not spend the refresh token."""
    ConnectionRepository(factory).mark_status(
        connection_id="conn-1", status=ConnectionStatus.ACTIVE, now=T0 - timedelta(hours=1)
    )
    oauth = _StubOAuthClient(_fresh_tokens())

    worker = _run(factory, _handler(factory, oauth))

    assert worker.stats.succeeded == 1
    assert oauth.refreshed_with == []
    assert _connection(factory)[0] == ConnectionStatus.ACTIVE.value


def test_without_a_refresh_token_only_reconsent_can_help(factory: sessionmaker[Session]) -> None:
    _reseal(factory, {"access_token": "long-lived"})
    oauth = _StubOAuthClient(_fresh_tokens())

    worker = _run(factory, _handler(factory, oauth))

    assert worker.stats.succeeded == 1
    assert oauth.refreshed_with == []
    # Nothing to learn, so nothing was re-stamped either.
    assert _connection(factory) == (
        ConnectionStatus.NEEDS_REAUTH.value,
        to_utc_rfc3339(PARKED_AT),
        "unauthorized",
    )


def test_an_unconfigured_provider_is_logged_not_raised(factory: sessionmaker[Session]) -> None:
    worker = _run(factory, _handler(factory, _StubOAuthClient(_fresh_tokens()), clients={}))

    assert worker.stats.succeeded == 1
    assert _connection(factory)[0] == ConnectionStatus.NEEDS_REAUTH.value


def test_an_unknown_connection_is_a_permanent_error(factory: sessionmaker[Session]) -> None:
    worker = _run(
        factory,
        _handler(factory, _StubOAuthClient(_fresh_tokens())),
        payload='{"connection_id":"nope"}',
    )
    assert worker.stats.dead_lettered == 1
