"""``GET /v1/data-quality`` over real HTTP.

Answers "is my data flowing", never "what does my data say" — findings carry
codes, severities, and provider names, no health values.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import Connection, ConnectionHealth, Tenant, User
from akunaki.adapters.db.session_repository import SessionRepository
from akunaki.api.app import create_app
from akunaki.api.security import SESSION_COOKIE_NAME
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339

NOW_S = to_utc_rfc3339(datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC))


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def route_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "data_quality.db"
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
def factory(route_db: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=route_db))
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
        session.add(
            User(
                id="user-1",
                tenant_id="tenant-1",
                oidc_issuer="https://idp.example.com",
                oidc_subject="subject-1",
                email=None,
                created_at=NOW_S,
            )
        )
    try:
        yield session_factory
    finally:
        engine.dispose()


@pytest.fixture
def client(route_db: str) -> TestClient:
    return TestClient(create_app(Settings(database_url=route_db)))


def _login(client: TestClient, factory: sessionmaker[Session]) -> None:
    issued = SessionRepository(factory).issue(
        session_id="sess-user-1",
        user_id="user-1",
        now=datetime.now(UTC),
        ttl=timedelta(hours=12),
    )
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, issued.token)


def _add_connection(
    factory: sessionmaker[Session],
    *,
    connection_id: str = "conn-1",
    provider: str = "oura",
    status: str = "active",
    last_success_at: str | None = None,
    consecutive_failures: int = 0,
    tenant_id: str = "tenant-1",
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
        session.flush()
        session.add(
            ConnectionHealth(
                connection_id=connection_id,
                tenant_id=tenant_id,
                last_success_at=last_success_at,
                last_error_class=None,
                consecutive_failures=consecutive_failures,
            )
        )


def _codes(client: TestClient) -> list[str]:
    return [f["code"] for f in client.get("/v1/data-quality").json()["findings"]]


def test_requires_a_session() -> None:
    client = TestClient(create_app(Settings(database_url="sqlite+libsql:///:memory:")))
    assert client.get("/v1/data-quality").status_code == 401


def test_no_connections_is_reported(client: TestClient, factory: sessionmaker[Session]) -> None:
    """A new user needs to know *why* there is no data."""
    _login(client, factory)

    assert _codes(client) == ["no_connections_linked"]


def test_a_healthy_connection_reports_nothing(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """No finding is the healthy answer; noise would train users to ignore it."""
    _add_connection(factory, last_success_at=to_utc_rfc3339(datetime.now(UTC)))
    _login(client, factory)

    assert client.get("/v1/data-quality").json() == {"findings": []}


def test_reauth_needed_is_reported_as_an_error(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    _add_connection(factory, status="needs_reauth")
    _login(client, factory)

    [finding] = client.get("/v1/data-quality").json()["findings"]

    assert finding["code"] == "connection_needs_reauth"
    assert finding["severity"] == "error"
    assert finding["provider"] == "oura"


def test_stale_connection_is_reported(client: TestClient, factory: sessionmaker[Session]) -> None:
    """Days of silence is a standing problem, not a per-day gap."""
    long_ago = to_utc_rfc3339(datetime.now(UTC) - timedelta(days=5))
    _add_connection(factory, last_success_at=long_ago)
    _login(client, factory)

    assert "connection_stale_sync" in _codes(client)


def test_never_synced_connection_is_reported(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    _add_connection(factory, last_success_at=None)
    _login(client, factory)

    assert _codes(client) == ["connection_never_synced"]


def test_findings_carry_no_health_values(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """This surface answers 'is my data flowing', never 'what does it say'."""
    _add_connection(factory, status="needs_reauth")
    _login(client, factory)

    raw = client.get("/v1/data-quality").text.lower()

    for token in ("hrv", "sleep", "score", "steps", "heart"):
        assert token not in raw


def test_errors_sort_before_warnings(client: TestClient, factory: sessionmaker[Session]) -> None:
    """A truncated list must show what matters."""
    long_ago = to_utc_rfc3339(datetime.now(UTC) - timedelta(days=5))
    _add_connection(factory, connection_id="c1", provider="polar", last_success_at=long_ago)
    _add_connection(factory, connection_id="c2", provider="oura", status="needs_reauth")
    _login(client, factory)

    severities = [f["severity"] for f in client.get("/v1/data-quality").json()["findings"]]

    assert severities == ["error", "warning"]


def test_never_reports_another_tenants_connections(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
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
    _add_connection(factory, connection_id="theirs", status="needs_reauth", tenant_id="tenant-2")
    _login(client, factory)

    # tenant-1 has no connections of its own.
    assert _codes(client) == ["no_connections_linked"]
