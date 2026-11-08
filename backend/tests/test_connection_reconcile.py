"""Reconcile support: last_success_at on ACTIVE, and the stale query.

Runs against a migrated database through the real ConnectionRepository.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.crypto.envelope import KEY_BYTES, EnvelopeSealer
from akunaki.adapters.db.connection_repository import ConnectionRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import ConnectionHealth, Tenant
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.connections import ConnectionStatus, Provider
from akunaki.domain.jobs import to_utc_rfc3339
from conftest import upgrade_to_head

T0 = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
KEK = b"\x55" * KEY_BYTES


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "reconcile.db"
    url = f"sqlite+libsql:///{db_path.resolve()}"
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", url)
    clear_settings_cache()
    upgrade_to_head(url)
    yield url
    clear_settings_cache()


@pytest.fixture
def factory(db_url: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=db_url))
    session_factory = create_session_factory(engine)
    # A connection is unique per (tenant, provider), so each connection under
    # test gets its own tenant to keep them independent.
    with session_factory() as session, session.begin():
        for i in range(12):
            session.add(
                Tenant(
                    id=f"tenant-{i}",
                    created_at=NOW_S,
                    status="active",
                    primary_timezone="UTC",
                    display_name=f"Test {i}",
                )
            )
    try:
        yield session_factory
    finally:
        engine.dispose()


def _link(
    repo: ConnectionRepository,
    *,
    connection_id: str,
    tenant_id: str,
    now: datetime,
    provider: Provider = Provider.OURA,
) -> None:
    sealer = EnvelopeSealer(keys={"v1": KEK}, active_key_version="v1")
    repo.link(
        connection_id=connection_id,
        tenant_id=tenant_id,
        provider=provider,
        sealed_secret=sealer.seal(b'{"access_token":"at"}', aad=connection_id.encode()),
        scopes=("daily",),
        external_user_id=None,
        now=now,
    )


def test_mark_active_records_last_success_at(factory: sessionmaker[Session]) -> None:
    repo = ConnectionRepository(factory)
    _link(repo, connection_id="conn-1", tenant_id="tenant-0", now=T0)

    synced_at = T0 + timedelta(hours=1)
    repo.mark_status(connection_id="conn-1", status=ConnectionStatus.ACTIVE, now=synced_at)

    with factory() as session:
        health = session.get(ConnectionHealth, "conn-1")
    assert health is not None
    assert health.last_success_at == to_utc_rfc3339(synced_at)
    assert health.consecutive_failures == 0


def test_success_clears_a_prior_failure(factory: sessionmaker[Session]) -> None:
    repo = ConnectionRepository(factory)
    _link(repo, connection_id="conn-1", tenant_id="tenant-1", now=T0)
    # An error is recorded first...
    repo.mark_status(
        connection_id="conn-1",
        status=ConnectionStatus.ERROR,
        now=T0 + timedelta(hours=1),
        error_class="rate_limit",
    )
    # ...then a later success clears the error class and streak.
    repo.mark_status(
        connection_id="conn-1", status=ConnectionStatus.ACTIVE, now=T0 + timedelta(hours=2)
    )
    with factory() as session:
        health = session.get(ConnectionHealth, "conn-1")
    assert health is not None
    assert health.last_error_class is None
    assert health.consecutive_failures == 0


def test_stale_query_finds_never_synced_and_old(factory: sessionmaker[Session]) -> None:
    repo = ConnectionRepository(factory)
    # fresh: synced just now
    _link(repo, connection_id="fresh", tenant_id="tenant-2", now=T0)
    repo.mark_status(connection_id="fresh", status=ConnectionStatus.ACTIVE, now=T0)
    # stale-old: last successful sync a day ago
    _link(repo, connection_id="old", tenant_id="tenant-3", now=T0)
    repo.mark_status(
        connection_id="old", status=ConnectionStatus.ACTIVE, now=T0 - timedelta(days=1)
    )
    # stale-since-link: linked long ago, never synced since (link-time success is old)
    _link(repo, connection_id="stale-link", tenant_id="tenant-4", now=T0 - timedelta(days=2))
    # inactive: needs_reauth must not be swept (it cannot sync until re-consent)
    _link(repo, connection_id="reauth", tenant_id="tenant-5", now=T0 - timedelta(days=2))
    repo.mark_status(connection_id="reauth", status=ConnectionStatus.NEEDS_REAUTH, now=T0)
    # error, settled long ago: a transient failure that exhausted its budget is
    # retried by the sweep once the staleness window has passed...
    _link(repo, connection_id="error-old", tenant_id="tenant-6", now=T0 - timedelta(days=2))
    repo.mark_status(
        connection_id="error-old",
        status=ConnectionStatus.ERROR,
        now=T0 - timedelta(days=1),
        error_class="provider_error",
    )
    # ...but one that just failed is not hammered on every tick.
    _link(repo, connection_id="error-fresh", tenant_id="tenant-7", now=T0 - timedelta(days=2))
    repo.mark_status(
        connection_id="error-fresh",
        status=ConnectionStatus.ERROR,
        now=T0 - timedelta(minutes=5),
        error_class="rate_limit",
    )

    cutoff = to_utc_rfc3339(T0 - timedelta(hours=6))
    stale = repo.stale_connections(cutoff=cutoff)
    ids = {cid for cid, _, _ in stale}
    assert ids == {"old", "stale-link", "error-old"}
    # tenant is carried for each.
    assert {tid for _, tid, _ in stale} == {"tenant-3", "tenant-4", "tenant-6"}
    # provider is carried too, so the sweep can fan out per stream.
    assert {provider for _, _, provider in stale} == {"oura"}


def test_stale_query_respects_limit(factory: sessionmaker[Session]) -> None:
    repo = ConnectionRepository(factory)
    for i in range(5):
        _link(repo, connection_id=f"c-{i}", tenant_id=f"tenant-{i}", now=T0 - timedelta(days=1))
    cutoff = to_utc_rfc3339(T0)
    assert len(repo.stale_connections(cutoff=cutoff, limit=3)) == 3


# ---------------------------------------------------------------------------
# Every successful window stamps health — not only the initial backfill
# ---------------------------------------------------------------------------


def test_record_sync_success_stamps_health_and_restores_error(
    factory: sessionmaker[Session],
) -> None:
    """Until 2026-11-08 only the initial sync wrote ``last_success_at``.

    Every connection then read as last-synced on its link day for as long as
    it lived, and one dead-lettered fetch left ``status = error`` for good
    while thousands of later windows succeeded.
    """
    repo = ConnectionRepository(factory)
    _link(repo, connection_id="conn-1", tenant_id="tenant-0", now=T0)
    repo.mark_status(
        connection_id="conn-1",
        status=ConnectionStatus.ERROR,
        now=T0 + timedelta(hours=1),
        error_class="provider_error",
    )

    synced_at = T0 + timedelta(hours=2)
    assert repo.record_sync_success(connection_id="conn-1", now=synced_at) is True

    connection = repo.get_connection(connection_id="conn-1")
    assert connection is not None
    assert connection.status is ConnectionStatus.ACTIVE
    with factory() as session:
        health = session.get(ConnectionHealth, "conn-1")
    assert health is not None
    assert health.last_success_at == to_utc_rfc3339(synced_at)
    assert health.last_error_class is None
    assert health.consecutive_failures == 0


def test_record_sync_success_leaves_needs_reauth_and_revoked_alone(
    factory: sessionmaker[Session],
) -> None:
    """A secondary stream can succeed on a token the primary's grant refuses.

    Only a refresh verdict lifts ``needs_reauth``; nothing undoes ``revoked``.
    The success is still stamped on health, since it did happen.
    """
    repo = ConnectionRepository(factory)
    for connection_id, tenant_id, status in (
        ("parked", "tenant-1", ConnectionStatus.NEEDS_REAUTH),
        ("gone", "tenant-2", ConnectionStatus.REVOKED),
    ):
        _link(repo, connection_id=connection_id, tenant_id=tenant_id, now=T0)
        repo.mark_status(connection_id=connection_id, status=status, now=T0)

        assert repo.record_sync_success(connection_id=connection_id, now=T0 + timedelta(hours=1))

        connection = repo.get_connection(connection_id=connection_id)
        assert connection is not None
        assert connection.status is status
        with factory() as session:
            health = session.get(ConnectionHealth, connection_id)
        assert health is not None
        assert health.last_success_at == to_utc_rfc3339(T0 + timedelta(hours=1))


def test_record_sync_success_on_an_unknown_connection_is_false(
    factory: sessionmaker[Session],
) -> None:
    assert ConnectionRepository(factory).record_sync_success(connection_id="nope", now=T0) is False


def test_record_sync_success_makes_a_fresh_connection_not_stale(
    factory: sessionmaker[Session],
) -> None:
    """The stamp is what keeps the sweep from refetching a connection every tick."""
    repo = ConnectionRepository(factory)
    _link(repo, connection_id="conn-1", tenant_id="tenant-3", now=T0 - timedelta(days=2))
    cutoff = to_utc_rfc3339(T0 - timedelta(hours=6))
    assert {cid for cid, _, _ in repo.stale_connections(cutoff=cutoff)} == {"conn-1"}

    repo.record_sync_success(connection_id="conn-1", now=T0)

    assert repo.stale_connections(cutoff=cutoff) == []


# ---------------------------------------------------------------------------
# Reauth candidates
# ---------------------------------------------------------------------------


def test_reauth_candidates_are_parked_connections_not_probed_recently(
    factory: sessionmaker[Session],
) -> None:
    repo = ConnectionRepository(factory)
    # Parked a day ago: due for a probe.
    _link(repo, connection_id="due", tenant_id="tenant-4", now=T0 - timedelta(days=2))
    repo.mark_status(
        connection_id="due", status=ConnectionStatus.NEEDS_REAUTH, now=T0 - timedelta(days=1)
    )
    # Probed (re-stamped) an hour ago: wait for the interval.
    _link(repo, connection_id="recent", tenant_id="tenant-5", now=T0 - timedelta(days=2))
    repo.mark_status(
        connection_id="recent", status=ConnectionStatus.NEEDS_REAUTH, now=T0 - timedelta(hours=1)
    )
    # Not parked at all.
    _link(repo, connection_id="fine", tenant_id="tenant-6", now=T0 - timedelta(days=2))
    _link(repo, connection_id="errored", tenant_id="tenant-7", now=T0 - timedelta(days=2))
    repo.mark_status(
        connection_id="errored",
        status=ConnectionStatus.ERROR,
        now=T0 - timedelta(days=1),
        error_class="provider_error",
    )

    cutoff = to_utc_rfc3339(T0 - timedelta(hours=6))
    candidates = repo.reauth_candidates(cutoff=cutoff)

    assert candidates == [("due", "tenant-4", "oura")]
    assert len(repo.reauth_candidates(cutoff=cutoff, limit=0)) == 0
