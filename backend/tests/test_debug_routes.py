"""Internal debug surface: the phase-one vertical slice, served over HTTP.

The router is unauthenticated and serves tenant health data, so "off unless
explicitly enabled" is a security property, not a preference.
"""

from __future__ import annotations

import json
from collections.abc import Generator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.crypto.envelope import KEY_BYTES, EnvelopeSealer
from akunaki.adapters.db.connection_repository import ConnectionRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.fact_repository import FactRepository
from akunaki.adapters.db.models import Tenant
from akunaki.api.app import create_app
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.connections import Provider
from akunaki.domain.jobs import to_utc_rfc3339
from akunaki.domain.sleep_normalizer import normalize_sleep_payload

T0 = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
KEK = b"\x88" * KEY_BYTES


def _sleep_page(vendor_id: str, *, end: str, total: int) -> str:
    return json.dumps(
        {
            "data": [
                {
                    "id": vendor_id,
                    "bedtime_start": "2026-07-18T23:00:00+02:00",
                    "bedtime_end": end,
                    "total_sleep_duration": total,
                    "type": "long_sleep",
                }
            ]
        }
    )


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def debug_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "debug.db"
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
def factory(debug_db: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=debug_db))
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


def _enabled_client(debug_db: str) -> TestClient:
    settings = Settings(database_url=debug_db, debug_routes_enabled=True)
    return TestClient(create_app(settings))


def _populate(factory: sessionmaker[Session]) -> None:
    """A linked connection plus one normalized sleep fact."""
    sealer = EnvelopeSealer(keys={"v1": KEK}, active_key_version="v1")
    ConnectionRepository(factory).link(
        connection_id="conn-1",
        tenant_id="tenant-1",
        provider=Provider.OURA,
        sealed_secret=sealer.seal(json.dumps({"access_token": "AT"}).encode(), aad=b"conn-1"),
        scopes=("daily",),
        external_user_id=None,
        now=T0,
    )
    [fact] = normalize_sleep_payload(
        _sleep_page("sleep-1", end="2026-07-19T07:00:00+02:00", total=27000)
    )
    FactRepository(factory).write_sleep_fact(
        fact_record_id="fact-1",
        tenant_id="tenant-1",
        connection_id="conn-1",
        fact=fact,
        raw_revision_id=None,
        raw_payload_id=None,
        schema_version="oura.v2",
        now=T0,
    )


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------


def test_debug_routes_are_absent_by_default(debug_db: str) -> None:
    """Unauthenticated health data must not be served unless asked for."""
    client = TestClient(create_app(Settings(database_url=debug_db)))

    assert client.get("/internal/debug/sync-status?tenant_id=tenant-1").status_code == 404


def test_debug_routes_are_not_even_registered_by_default(debug_db: str) -> None:
    """Absent from the schema, not merely guarded at request time."""
    app = create_app(Settings(database_url=debug_db))
    assert not [p for p in app.openapi()["paths"] if p.startswith("/internal")]


def test_default_settings_disable_debug_routes() -> None:
    assert Settings().debug_routes_enabled is False


def test_enabling_mounts_the_router(debug_db: str) -> None:
    app = create_app(Settings(database_url=debug_db, debug_routes_enabled=True))
    paths = app.openapi()["paths"]
    assert "/internal/debug/sync-status" in paths
    # latest-sleep was removed: it is superseded by the authenticated /v1/sleep.
    assert "/internal/debug/latest-sleep" not in paths


# ---------------------------------------------------------------------------
# The vertical slice
# ---------------------------------------------------------------------------


def test_sync_status_reports_connection_progress(
    factory: sessionmaker[Session], debug_db: str
) -> None:
    _populate(factory)
    response = _enabled_client(debug_db).get(
        "/internal/debug/sync-status", params={"tenant_id": "tenant-1"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == "tenant-1"
    [connection] = body["connections"]
    assert connection["provider"] == "oura"
    assert connection["status"] == "active"
    assert connection["consecutive_failures"] == 0


# ---------------------------------------------------------------------------
# Tenant scoping and absence
# ---------------------------------------------------------------------------


def test_other_tenants_data_is_not_returned(factory: sessionmaker[Session], debug_db: str) -> None:
    """Cross-tenant reads must be indistinguishable from 'no data'."""
    _populate(factory)
    client = _enabled_client(debug_db)

    assert (
        client.get("/internal/debug/sync-status", params={"tenant_id": "other"}).json()[
            "connections"
        ]
        == []
    )


def test_tenant_id_is_required(debug_db: str) -> None:
    client = _enabled_client(debug_db)
    assert client.get("/internal/debug/sync-status", params={"tenant_id": ""}).status_code == 422


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_status_responses_are_never_cached(factory: sessionmaker[Session], debug_db: str) -> None:
    _populate(factory)
    response = _enabled_client(debug_db).get(
        "/internal/debug/sync-status", params={"tenant_id": "tenant-1"}
    )
    assert response.headers["cache-control"] == "private, no-store"
