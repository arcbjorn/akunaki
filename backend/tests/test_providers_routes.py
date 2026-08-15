"""``GET /v1/providers`` over real HTTP.

Two properties carry the weight: an **unconfigured provider stays invisible**
(the authorize route's 404 must not be probeable from here), and the
capabilities describe what a connector *actually* ingests.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import Connection, Tenant, User
from akunaki.adapters.db.session_repository import SessionRepository
from akunaki.api.app import create_app
from akunaki.api.security import SESSION_COOKIE_NAME
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339
from conftest import upgrade_to_head

KEK_B64 = base64.b64encode(os.urandom(32)).decode()
REDIRECT = "https://app.example.com/callback"
NOW_S = to_utc_rfc3339(datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC))


def _settings(url: str) -> Settings:
    """Polar configured; Oura and Google Health deliberately are not."""
    return Settings(
        database_url=url,
        secret_keks=f"v1:{KEK_B64}",
        active_kek_version="v1",
        polar_client_id="pid",
        polar_client_secret="psecret",
        polar_redirect_uri=REDIRECT,
    )


@pytest.fixture
def route_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "providers_routes.db"
    url = f"sqlite+libsql:///{db_path.resolve()}"
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", url)
    clear_settings_cache()
    upgrade_to_head(url)
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
    return TestClient(create_app(_settings(route_db)))


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
    provider: str = "polar",
    status: str = "active",
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


def _providers(client: TestClient) -> list[dict[str, object]]:
    providers: list[dict[str, object]] = client.get("/v1/providers").json()["providers"]
    return providers


def test_requires_a_session(route_db: str) -> None:
    client = TestClient(create_app(_settings(route_db)))

    assert client.get("/v1/providers").status_code == 401


def test_lists_the_configured_provider(client: TestClient, factory: sessionmaker[Session]) -> None:
    _login(client, factory)

    [provider] = _providers(client)

    assert provider["provider"] == "polar"


def test_unconfigured_providers_stay_invisible(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """The authorize route's 404 must not be probeable from here.

    Oura and Google Health have linkable OAuth clients but no credentials in
    this deployment. Listing them would reveal what *could* be linked if
    credentials were set — exactly what ``_provider_config`` refuses to disclose.
    """
    _login(client, factory)

    names = {provider["provider"] for provider in _providers(client)}

    assert "oura" not in names
    assert "google_health" not in names


def test_capabilities_describe_what_is_ingested(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """Polar backfills workouts only — no sleep, no overnight vitals."""
    _login(client, factory)

    [provider] = _providers(client)

    assert provider["capabilities"] == ["workouts"]


def test_a_workout_only_provider_cannot_reach_a_recovery_score(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """The whole reason this surface exists.

    Linking Polar alone yields workouts and a permanently ``insufficient``
    score. Without this flag a user has no way to learn that before waiting.
    """
    _login(client, factory)

    [provider] = _providers(client)

    assert provider["supports_recovery_score"] is False


def test_an_unlinked_provider_has_no_connection_status(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    _login(client, factory)

    [provider] = _providers(client)

    assert provider["connection_status"] is None


def test_a_linked_provider_reports_its_status(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    _add_connection(factory, status="active")
    _login(client, factory)

    [provider] = _providers(client)

    assert provider["connection_status"] == "active"


def test_a_revoked_connection_is_reported_not_hidden(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """A revoke updates the row in place — the provider stays listed.

    ``uq_connections_tenant_provider`` allows only one row per provider, so a
    revoked connection is the live answer for that provider rather than a stale
    duplicate to skip. The user needs to see it to understand why data stopped.
    """
    _add_connection(factory, status="revoked")
    _login(client, factory)

    [provider] = _providers(client)

    assert provider["connection_status"] == "revoked"


def test_never_reports_another_tenants_connection(
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
    _add_connection(factory, connection_id="theirs", status="active", tenant_id="tenant-2")
    _login(client, factory)

    [provider] = _providers(client)

    assert provider["connection_status"] is None


def test_carries_no_health_values(client: TestClient, factory: sessionmaker[Session]) -> None:
    """Capability names and link status only — never measurements."""
    _add_connection(factory, status="active")
    _login(client, factory)

    [provider] = _providers(client)

    assert set(provider) == {
        "provider",
        "capabilities",
        "supports_recovery_score",
        "connection_status",
    }


def test_carries_no_credential_material(client: TestClient, factory: sessionmaker[Session]) -> None:
    """Configuration decides visibility; it must never be echoed."""
    _login(client, factory)

    raw = client.get("/v1/providers").text

    assert "psecret" not in raw
    assert "pid" not in raw
    assert REDIRECT not in raw
