"""Per-connection sync status backing the authenticated ``/v1/connections``.

The counts are the point: a tenant with two providers must see each one's real
ingest volume. Counting by tenant inside a per-connection loop reports the
tenant-wide total against every row, which reads as though a connection that
has fetched nothing is fully synced.
"""

from __future__ import annotations

import json
from collections.abc import Generator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.connection_repository import ConnectionRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import (
    Connection,
    ConnectionHealth,
    RawObject,
    RawPayload,
    RawRevision,
    Tenant,
)
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339

NOW_S = to_utc_rfc3339(datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC))


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def status_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "statuses.db"
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
def factory(status_db: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=status_db))
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


def _add_connection(
    factory: sessionmaker[Session],
    *,
    connection_id: str,
    provider: str,
    tenant_id: str = "tenant-1",
    status: str = "active",
) -> None:
    with factory() as session, session.begin():
        session.add(
            Connection(
                id=connection_id,
                tenant_id=tenant_id,
                provider=provider,
                status=status,
                scopes_granted_json="[]",
                external_user_id=None,
                connected_at=NOW_S,
                updated_at=NOW_S,
            )
        )


def _add_ingest(
    factory: sessionmaker[Session],
    *,
    connection_id: str,
    provider: str,
    pages: int,
    revisions: int,
    tenant_id: str = "tenant-1",
) -> None:
    """Seed ``pages`` transport rows and ``revisions`` logical records."""
    with factory() as session, session.begin():
        for n in range(pages):
            session.add(
                RawPayload(
                    id=f"{connection_id}-pay-{n}",
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    sync_run_id=None,
                    transport_kind="sync_fetch",
                    provider=provider,
                    stream="sleep",
                    page_token=None,
                    fetched_at=NOW_S,
                    received_at=NOW_S,
                    http_status=200,
                    content_type="application/json",
                    content_hash=f"{connection_id}-hash-{n}",
                    payload_json='{"data":[]}',
                    payload_blob=None,
                    request_meta_json=json.dumps({"url_template": "v2/sleep"}),
                )
            )
        for n in range(revisions):
            session.add(
                RawObject(
                    id=f"{connection_id}-obj-{n}",
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    provider=provider,
                    stream="sleep",
                    vendor_record_id=f"{connection_id}-vendor-{n}",
                    current_revision_id=None,
                    created_at=NOW_S,
                )
            )
            session.add(
                RawRevision(
                    id=f"{connection_id}-rev-{n}",
                    tenant_id=tenant_id,
                    raw_object_id=f"{connection_id}-obj-{n}",
                    raw_payload_id=f"{connection_id}-pay-0",
                    sync_run_id=None,
                    revision_n=1,
                    vendor_record_id=f"{connection_id}-vendor-{n}",
                    observed_at=NOW_S,
                    effective_at=NOW_S,
                    received_at=NOW_S,
                    content_hash=f"{connection_id}-rhash-{n}",
                    schema_version="oura.v2",
                    deletion_state="active",
                    is_tombstone=0,
                    tombstone_reason=None,
                )
            )


def test_no_connections_is_an_empty_list(factory: sessionmaker[Session]) -> None:
    assert ConnectionRepository(factory).connection_statuses(tenant_id="tenant-1") == []


def test_counts_are_scoped_to_their_own_connection(
    factory: sessionmaker[Session],
) -> None:
    """Each connection reports its own ingest volume, not the tenant total."""
    _add_connection(factory, connection_id="conn-oura", provider="oura")
    _add_connection(factory, connection_id="conn-polar", provider="polar")
    _add_ingest(factory, connection_id="conn-oura", provider="oura", pages=3, revisions=5)
    _add_ingest(factory, connection_id="conn-polar", provider="polar", pages=1, revisions=2)

    rows = ConnectionRepository(factory).connection_statuses(tenant_id="tenant-1")
    statuses = {s.provider: s for s in rows}

    assert statuses["oura"].transport_pages == 3
    assert statuses["oura"].raw_revisions == 5
    # Not 7 (the tenant-wide revision total), which a tenant-scoped count gives.
    assert statuses["polar"].transport_pages == 1
    assert statuses["polar"].raw_revisions == 2


def test_connection_with_no_ingest_reports_zero(factory: sessionmaker[Session]) -> None:
    """A freshly linked connection is honestly empty even beside a busy one."""
    _add_connection(factory, connection_id="conn-oura", provider="oura")
    _add_connection(factory, connection_id="conn-polar", provider="polar")
    _add_ingest(factory, connection_id="conn-oura", provider="oura", pages=4, revisions=6)

    rows = ConnectionRepository(factory).connection_statuses(tenant_id="tenant-1")
    statuses = {s.provider: s for s in rows}

    assert statuses["polar"].transport_pages == 0
    assert statuses["polar"].raw_revisions == 0


def test_health_is_reported_when_present(factory: sessionmaker[Session]) -> None:
    _add_connection(factory, connection_id="conn-oura", provider="oura", status="error")
    with factory() as session, session.begin():
        session.add(
            ConnectionHealth(
                connection_id="conn-oura",
                tenant_id="tenant-1",
                last_success_at=NOW_S,
                last_error_class="RateLimited",
                consecutive_failures=2,
            )
        )

    status = ConnectionRepository(factory).connection_statuses(tenant_id="tenant-1")[0]
    assert status.status == "error"
    assert status.last_success_at == NOW_S
    assert status.last_error_class == "RateLimited"
    assert status.consecutive_failures == 2


def test_missing_health_row_reads_as_never_synced(factory: sessionmaker[Session]) -> None:
    """A connection with no health row is reported, not dropped by the join."""
    _add_connection(factory, connection_id="conn-oura", provider="oura", status="pending")

    statuses = ConnectionRepository(factory).connection_statuses(tenant_id="tenant-1")
    assert len(statuses) == 1
    assert statuses[0].last_success_at is None
    assert statuses[0].last_error_class is None
    assert statuses[0].consecutive_failures == 0


def test_statuses_are_tenant_scoped(factory: sessionmaker[Session]) -> None:
    """Another tenant's connection and its counts stay invisible."""
    with factory() as session, session.begin():
        session.add(
            Tenant(
                id="tenant-2",
                created_at=NOW_S,
                status="active",
                primary_timezone="UTC",
                display_name="Other",
            )
        )
    _add_connection(factory, connection_id="conn-mine", provider="oura")
    _add_connection(factory, connection_id="conn-theirs", provider="polar", tenant_id="tenant-2")
    _add_ingest(
        factory,
        connection_id="conn-theirs",
        provider="polar",
        pages=9,
        revisions=9,
        tenant_id="tenant-2",
    )

    statuses = ConnectionRepository(factory).connection_statuses(tenant_id="tenant-1")
    assert [s.connection_id for s in statuses] == ["conn-mine"]
    assert statuses[0].raw_revisions == 0
