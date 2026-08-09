"""End-to-end coverage of the connector link routes over real HTTP.

Verify the authenticated authorize/callback legs link a connection for the
caller's tenant, that an unconfigured or unknown provider is a 404 (no
half-built connect surface), and that the routes require a session. Polar is the
configured provider; its token endpoint is served in-process by patching the
route's ``build_oauth_client`` to return a Polar client over a mock transport.
"""

from __future__ import annotations

import json
from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

import akunaki.api.routes.connections as connections_mod
from akunaki.adapters.connectors.polar import PolarOAuthClient
from akunaki.adapters.db.audit_repository import AuditRepository
from akunaki.adapters.db.models import (
    AuditEventRow,
    Connection,
    ConnectionSecret,
    FactRecord,
    Job,
    Tenant,
    User,
)
from akunaki.adapters.db.session_repository import SessionRepository
from akunaki.api.app import create_app
from akunaki.api.security import CSRF_HEADER_NAME, SESSION_COOKIE_NAME
from akunaki.config import ConnectorOAuthConfig, Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339

T0 = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
KEK_B64 = "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE="  # 32 bytes, base64
REDIRECT = "https://app.example.com/oauth/polar/callback"

POLAR_TOKEN_BODY = {
    "access_token": "polar-access-SECRET",
    "token_type": "bearer",
    "expires_in": 86400,
    "x_user_id": 555,
}


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _settings(url: str) -> Settings:
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
    db_path = tmp_path / "conn_routes.db"
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
    from akunaki.adapters.db.engine import create_db_engine, create_session_factory

    engine = create_db_engine(_settings(route_db))
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
def client(route_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # Patch the client factory so Polar's token endpoint is served in-process.
    def _mock_client(provider: str, config: ConnectorOAuthConfig) -> PolarOAuthClient:
        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=POLAR_TOKEN_BODY)

        return PolarOAuthClient(
            client_id=config.client_id,
            client_secret=config.client_secret,
            transport=httpx2.Client(transport=httpx2.MockTransport(handler)),
        )

    monkeypatch.setattr(connections_mod, "build_oauth_client", _mock_client)
    yield TestClient(create_app(_settings(route_db)))


def _login(client: TestClient, factory: sessionmaker[Session]) -> str:
    """Attach a session cookie and return its CSRF secret."""
    issued = SessionRepository(factory).issue(
        session_id="sess-user-1",
        user_id="user-1",
        now=datetime.now(UTC),
        ttl=timedelta(hours=12),
    )
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, issued.token)
    return str(issued.csrf_secret)


def test_authorize_returns_a_provider_url(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    _login(client, factory)
    resp = client.get("/v1/connections/polar/authorize")
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "polar"
    parsed = urlparse(body["authorize_url"])
    assert parsed.netloc == "flow.polar.com"
    assert "state" in parse_qs(parsed.query)


def test_full_link_flow_over_http(client: TestClient, factory: sessionmaker[Session]) -> None:
    _login(client, factory)
    # Authorize, capture the state, then complete the callback.
    authorize = client.get("/v1/connections/polar/authorize").json()
    state = parse_qs(urlparse(authorize["authorize_url"]).query)["state"][0]

    resp = client.get(
        "/v1/connections/polar/callback",
        params={"state": state, "code": "auth-code-1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "polar"
    assert body["status"] == "active"
    assert body["connection_id"]


def test_unknown_provider_is_404(client: TestClient, factory: sessionmaker[Session]) -> None:
    _login(client, factory)
    resp = client.get("/v1/connections/garmin/authorize")
    assert resp.status_code == 404


def test_unconfigured_provider_is_404(client: TestClient, factory: sessionmaker[Session]) -> None:
    # Oura is a supported provider but has no credentials configured here.
    _login(client, factory)
    resp = client.get("/v1/connections/oura/authorize")
    assert resp.status_code == 404


def test_authorize_requires_a_session(client: TestClient) -> None:
    client.cookies.clear()
    resp = client.get("/v1/connections/polar/authorize")
    assert resp.status_code == 401


def test_list_requires_a_session(client: TestClient) -> None:
    """The surface this replaces was unauthenticated; this one is not."""
    client.cookies.clear()
    resp = client.get("/v1/connections")
    assert resp.status_code == 401


def test_list_is_empty_before_linking(client: TestClient, factory: sessionmaker[Session]) -> None:
    # A user who has linked nothing has no connections: a real answer, not a 404.
    _login(client, factory)
    resp = client.get("/v1/connections")
    assert resp.status_code == 200
    assert resp.json() == {"connections": []}


def test_list_shows_a_linked_connection(client: TestClient, factory: sessionmaker[Session]) -> None:
    """A freshly linked provider is reported active with its sync status."""
    _login(client, factory)
    authorize = client.get("/v1/connections/polar/authorize").json()
    state = parse_qs(urlparse(authorize["authorize_url"]).query)["state"][0]
    linked = client.get(
        "/v1/connections/polar/callback",
        params={"state": state, "code": "auth-code-1"},
    ).json()

    resp = client.get("/v1/connections")
    assert resp.status_code == 200
    connections = resp.json()["connections"]
    assert len(connections) == 1
    row = connections[0]
    assert row["connection_id"] == linked["connection_id"]
    assert row["provider"] == "polar"
    assert row["status"] == "active"
    # Linking resets the failure streak and records a first success.
    assert row["consecutive_failures"] == 0
    assert row["last_error_class"] is None
    assert row["last_success_at"] is not None
    # Nothing has been fetched yet, so both ingest counts are honestly zero.
    assert row["transport_pages"] == 0
    assert row["raw_revisions"] == 0


def test_list_never_serves_another_tenant(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """Tenant comes from the session, so another tenant's rows stay invisible.

    The replaced debug route took ``tenant_id`` as a query parameter; supplying
    one here must change nothing.
    """
    _login(client, factory)
    authorize = client.get("/v1/connections/polar/authorize").json()
    state = parse_qs(urlparse(authorize["authorize_url"]).query)["state"][0]
    client.get(
        "/v1/connections/polar/callback",
        params={"state": state, "code": "auth-code-1"},
    )

    # A second tenant owning its own connection.
    with factory() as session, session.begin():
        session.add(
            Tenant(
                id="tenant-2",
                created_at=to_utc_rfc3339(datetime.now(UTC)),
                status="active",
                primary_timezone="UTC",
                display_name="Other",
            )
        )
        session.add(
            Connection(
                id="conn-other",
                tenant_id="tenant-2",
                provider="oura",
                status="active",
                scopes_granted_json="[]",
                external_user_id=None,
                connected_at=to_utc_rfc3339(datetime.now(UTC)),
                updated_at=to_utc_rfc3339(datetime.now(UTC)),
            )
        )

    # Even asking for the other tenant by parameter yields only the caller's own.
    resp = client.get("/v1/connections", params={"tenant_id": "tenant-2"})
    assert resp.status_code == 200
    providers = [c["provider"] for c in resp.json()["connections"]]
    assert providers == ["polar"]


# ---------------------------------------------------------------------------
# Manual sync
# ---------------------------------------------------------------------------


def _link_polar(client: TestClient) -> str:
    """Walk the real OAuth legs and return the linked connection id."""
    authorize = client.get("/v1/connections/polar/authorize").json()
    state = parse_qs(urlparse(authorize["authorize_url"]).query)["state"][0]
    return str(
        client.get(
            "/v1/connections/polar/callback",
            params={"state": state, "code": "auth-code-1"},
        ).json()["connection_id"]
    )


def test_sync_requires_a_session(client: TestClient) -> None:
    client.cookies.clear()
    response = client.post("/v1/connections/conn-1/sync", headers={"Idempotency-Key": "k1"})
    assert response.status_code == 401


def test_sync_requires_csrf(client: TestClient, factory: sessionmaker[Session]) -> None:
    """Enqueuing work is state-changing, so a bare cookie is not enough."""
    _login(client, factory)
    connection_id = _link_polar(client)

    response = client.post(
        f"/v1/connections/{connection_id}/sync", headers={"Idempotency-Key": "k1"}
    )

    assert response.status_code == 403


def test_sync_requires_an_idempotency_key(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """Without a key a retried click would queue a second job."""
    csrf = _login(client, factory)
    connection_id = _link_polar(client)

    response = client.post(
        f"/v1/connections/{connection_id}/sync", headers={CSRF_HEADER_NAME: csrf}
    )

    assert response.status_code == 422


def test_sync_enqueues_an_incremental_sync(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """The queued job is the same one webhooks and the sweep use."""
    csrf = _login(client, factory)
    connection_id = _link_polar(client)

    body = client.post(
        f"/v1/connections/{connection_id}/sync",
        headers={CSRF_HEADER_NAME: csrf, "Idempotency-Key": "k1"},
    ).json()

    assert body["created"] is True
    with factory() as session:
        job = session.scalars(
            select(Job).where(Job.job_type == "connection.incremental_sync")
        ).one()
    assert job.tenant_id == "tenant-1"
    assert connection_id in job.payload_json


def test_repeated_sync_request_is_deduped(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """A double-clicked button queues one job, not two."""
    csrf = _login(client, factory)
    connection_id = _link_polar(client)
    headers = {CSRF_HEADER_NAME: csrf, "Idempotency-Key": "k1"}

    first = client.post(f"/v1/connections/{connection_id}/sync", headers=headers).json()
    second = client.post(f"/v1/connections/{connection_id}/sync", headers=headers).json()

    assert first["created"] is True
    assert second["created"] is False
    assert second["job_id"] == first["job_id"]
    with factory() as session:
        jobs = session.scalars(
            select(Job).where(Job.job_type == "connection.incremental_sync")
        ).all()
    assert len(jobs) == 1


def test_sync_on_an_unknown_connection_is_404(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    csrf = _login(client, factory)

    response = client.post(
        "/v1/connections/nope/sync",
        headers={CSRF_HEADER_NAME: csrf, "Idempotency-Key": "k1"},
    )

    assert response.status_code == 404


def test_sync_on_another_tenants_connection_is_the_same_404(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """Cross-tenant and unknown must be indistinguishable, and queue nothing."""
    csrf = _login(client, factory)
    with factory() as session, session.begin():
        session.add(
            Tenant(
                id="tenant-2",
                created_at=to_utc_rfc3339(datetime.now(UTC)),
                status="active",
                primary_timezone="UTC",
                display_name="Other",
            )
        )
        session.add(
            Connection(
                id="conn-theirs",
                tenant_id="tenant-2",
                provider="oura",
                status="active",
                scopes_granted_json="[]",
                external_user_id=None,
                connected_at=to_utc_rfc3339(datetime.now(UTC)),
                updated_at=to_utc_rfc3339(datetime.now(UTC)),
            )
        )

    theirs = client.post(
        "/v1/connections/conn-theirs/sync",
        headers={CSRF_HEADER_NAME: csrf, "Idempotency-Key": "k1"},
    )
    unknown = client.post(
        "/v1/connections/nope/sync",
        headers={CSRF_HEADER_NAME: csrf, "Idempotency-Key": "k2"},
    )

    assert theirs.status_code == unknown.status_code == 404
    assert theirs.json() == unknown.json()
    with factory() as session:
        assert session.scalars(select(Job)).all() == []


def test_sync_on_a_reauth_needing_connection_is_409(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """A connection that cannot sync says so, rather than queuing a doomed job."""
    csrf = _login(client, factory)
    connection_id = _link_polar(client)
    with factory() as session, session.begin():
        session.execute(
            update(Connection).where(Connection.id == connection_id).values(status="needs_reauth")
        )

    response = client.post(
        f"/v1/connections/{connection_id}/sync",
        headers={CSRF_HEADER_NAME: csrf, "Idempotency-Key": "k1"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "connection_not_syncable"
    with factory() as session:
        assert session.scalars(select(Job)).all() == []


# ---------------------------------------------------------------------------
# connection.create auditing
# ---------------------------------------------------------------------------


def test_successful_link_is_audited(client: TestClient, factory: sessionmaker[Session]) -> None:
    _login(client, factory)
    connection_id = _link_polar(client)

    with factory() as session:
        [event] = session.scalars(select(AuditEventRow)).all()

    assert event.action == "connection.create"
    assert event.resource_type == "connection"
    assert event.resource_id == connection_id
    assert event.tenant_id == "tenant-1"
    assert json.loads(event.metadata_json) == {"provider": "polar", "outcome": "linked"}


def test_failed_link_is_audited_too(client: TestClient, factory: sessionmaker[Session]) -> None:
    """ "Someone tried to link here" is what a reviewer needs after a bad callback.

    A failed exchange produces no connection, so the event is attributed to the
    session's tenant rather than a row that does not exist.
    """
    _login(client, factory)

    response = client.get(
        "/v1/connections/polar/callback",
        params={"state": "never-issued", "code": "auth-code-1"},
    )

    assert response.status_code >= 400
    with factory() as session:
        [event] = session.scalars(select(AuditEventRow)).all()
    assert event.action == "connection.create"
    assert event.resource_id is None
    assert json.loads(event.metadata_json)["outcome"] == "failed"


def test_link_audit_carries_no_token_material(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """The trail records that a link happened, never the credentials."""
    _login(client, factory)
    _link_polar(client)

    with factory() as session:
        [event] = session.scalars(select(AuditEventRow)).all()

    blob = event.metadata_json.lower()
    for token in ("access_token", "refresh", "secret", "code", "bearer"):
        assert token not in blob


# ---------------------------------------------------------------------------
# Disconnect
# ---------------------------------------------------------------------------


def test_disconnect_requires_a_session(client: TestClient) -> None:
    client.cookies.clear()
    assert client.delete("/v1/connections/conn-1").status_code == 401


def test_disconnect_requires_csrf(client: TestClient, factory: sessionmaker[Session]) -> None:
    """Revoking credentials is state-changing, so a bare cookie is not enough."""
    _login(client, factory)
    connection_id = _link_polar(client)

    assert client.delete(f"/v1/connections/{connection_id}").status_code == 403


def test_disconnect_deletes_the_stored_secret(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """The point of disconnecting: this system can no longer use the grant."""
    csrf = _login(client, factory)
    connection_id = _link_polar(client)
    with factory() as session:
        assert session.get(ConnectionSecret, connection_id) is not None

    response = client.delete(f"/v1/connections/{connection_id}", headers={CSRF_HEADER_NAME: csrf})

    assert response.status_code == 200
    assert response.json() == {"connection_id": connection_id, "status": "revoked"}
    with factory() as session:
        assert session.get(ConnectionSecret, connection_id) is None
        assert session.get(Connection, connection_id).status == "revoked"


def test_disconnect_preserves_history(client: TestClient, factory: sessionmaker[Session]) -> None:
    """Decided 2026-07-19: disconnect revokes credentials, never destroys facts.

    Only an explicit privacy delete removes health data.
    """
    csrf = _login(client, factory)
    connection_id = _link_polar(client)
    with factory() as session, session.begin():
        session.add(
            FactRecord(
                id="fact-1",
                tenant_id="tenant-1",
                connection_id=connection_id,
                provider="polar",
                entity_type="workout_session",
                vendor_record_id="w1",
                origin=None,
                method="wearable",
                utc_instant=NOW_S,
                start_utc=NOW_S,
                end_utc=NOW_S,
                source_offset_minutes=0,
                iana_timezone="UTC",
                local_health_day="2026-08-04",
                unit=None,
                quality="high",
                confidence=1.0,
                freshness_at=NOW_S,
                raw_revision_id=None,
                raw_payload_id=None,
                schema_version="polar.v1",
                normalizer_version="polar_workout_v0.1.0",
                content_hash="h1",
                fact_key="workout_session:w1",
                version_n=1,
                is_current=1,
                superseded_by=None,
                superseded_at=None,
                deletion_state="active",
                exclude_from_load=0,
                created_at=NOW_S,
            )
        )

    client.delete(f"/v1/connections/{connection_id}", headers={CSRF_HEADER_NAME: csrf})

    with factory() as session:
        assert session.get(FactRecord, "fact-1") is not None


def test_disconnected_connection_still_listed(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """A revoked connection stays visible so the user can see what happened."""
    csrf = _login(client, factory)
    connection_id = _link_polar(client)
    client.delete(f"/v1/connections/{connection_id}", headers={CSRF_HEADER_NAME: csrf})

    [row] = client.get("/v1/connections").json()["connections"]

    assert row["connection_id"] == connection_id
    assert row["status"] == "revoked"


def test_disconnected_connection_cannot_sync(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """A revoked connection has no tokens, so a sync would be doomed."""
    csrf = _login(client, factory)
    connection_id = _link_polar(client)
    client.delete(f"/v1/connections/{connection_id}", headers={CSRF_HEADER_NAME: csrf})

    response = client.post(
        f"/v1/connections/{connection_id}/sync",
        headers={CSRF_HEADER_NAME: csrf, "Idempotency-Key": "k1"},
    )

    assert response.status_code == 409
    with factory() as session:
        assert session.scalars(select(Job)).all() == []


def test_disconnecting_another_tenants_connection_is_404(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """Cross-tenant and unknown are indistinguishable, and nothing is revoked."""
    csrf = _login(client, factory)
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
        session.add(
            Connection(
                id="conn-theirs",
                tenant_id="tenant-2",
                provider="oura",
                status="active",
                scopes_granted_json="[]",
                external_user_id=None,
                connected_at=NOW_S,
                updated_at=NOW_S,
            )
        )

    theirs = client.delete("/v1/connections/conn-theirs", headers={CSRF_HEADER_NAME: csrf})
    unknown = client.delete("/v1/connections/nope", headers={CSRF_HEADER_NAME: csrf})

    assert theirs.status_code == unknown.status_code == 404
    assert theirs.json() == unknown.json()
    with factory() as session:
        assert session.get(Connection, "conn-theirs").status == "active"


def test_disconnect_is_audited(client: TestClient, factory: sessionmaker[Session]) -> None:
    csrf = _login(client, factory)
    connection_id = _link_polar(client)
    client.delete(f"/v1/connections/{connection_id}", headers={CSRF_HEADER_NAME: csrf})

    with factory() as session:
        events = session.scalars(
            select(AuditEventRow).where(AuditEventRow.action == "connection.revoke")
        ).all()

    assert len(events) == 1
    assert events[0].resource_id == connection_id
    assert json.loads(events[0].metadata_json) == {"outcome": "revoked"}


# ---------------------------------------------------------------------------
# connection.sync auditing
# ---------------------------------------------------------------------------


def _sync_events(factory: sessionmaker[Session]) -> list[AuditEventRow]:
    """Only the sync events; linking writes a connection.create event first."""
    with factory() as session:
        return [
            event
            for event in session.scalars(select(AuditEventRow)).all()
            if event.action == "connection.sync"
        ]


def test_sync_request_is_audited(client: TestClient, factory: sessionmaker[Session]) -> None:
    """A manual sync is a state change a user made; link and revoke both audit."""
    csrf = _login(client, factory)
    connection_id = _link_polar(client)

    client.post(
        f"/v1/connections/{connection_id}/sync",
        headers={CSRF_HEADER_NAME: csrf, "Idempotency-Key": "k1"},
    )

    [event] = _sync_events(factory)
    assert event.resource_type == "connection"
    assert event.resource_id == connection_id
    assert event.tenant_id == "tenant-1"
    assert json.loads(event.metadata_json) == {"outcome": "queued"}


def test_a_deduplicated_sync_is_audited_distinctly(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """Recording a collapsed retry as a queue would overstate vendor traffic."""
    csrf = _login(client, factory)
    connection_id = _link_polar(client)
    headers = {CSRF_HEADER_NAME: csrf, "Idempotency-Key": "k1"}

    client.post(f"/v1/connections/{connection_id}/sync", headers=headers)
    client.post(f"/v1/connections/{connection_id}/sync", headers=headers)

    outcomes = [json.loads(event.metadata_json)["outcome"] for event in _sync_events(factory)]
    assert outcomes == ["queued", "deduplicated"]


def test_a_refused_sync_is_audited_too(client: TestClient, factory: sessionmaker[Session]) -> None:
    """A reviewer investigating unexpected traffic needs attempts, not only wins."""
    csrf = _login(client, factory)

    response = client.post(
        "/v1/connections/does-not-exist/sync",
        headers={CSRF_HEADER_NAME: csrf, "Idempotency-Key": "k1"},
    )

    assert response.status_code == 404
    [event] = _sync_events(factory)
    assert json.loads(event.metadata_json) == {"outcome": "not_found"}


def test_an_unknown_sync_target_is_not_named_in_the_audit(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """The 404 hides whether an id exists; the audit trail must not reveal it."""
    csrf = _login(client, factory)

    client.post(
        "/v1/connections/probe-me/sync",
        headers={CSRF_HEADER_NAME: csrf, "Idempotency-Key": "k1"},
    )

    [event] = _sync_events(factory)
    assert event.resource_id is None


def test_sync_audit_carries_no_health_values(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    csrf = _login(client, factory)
    connection_id = _link_polar(client)

    client.post(
        f"/v1/connections/{connection_id}/sync",
        headers={CSRF_HEADER_NAME: csrf, "Idempotency-Key": "k1"},
    )

    [event] = _sync_events(factory)
    assert set(json.loads(event.metadata_json)) == {"outcome"}


def test_a_failed_sync_audit_does_not_fail_the_request(
    client: TestClient, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auditing is bookkeeping; the sync is already queued when it runs.

    Raising here would report a failure for work that succeeded, and the caller
    would retry a sync that is already on the queue.
    """
    csrf = _login(client, factory)
    connection_id = _link_polar(client)

    def boom(*_args: object, **_kwargs: object) -> None:
        msg = "database is locked"
        raise RuntimeError(msg)

    monkeypatch.setattr(AuditRepository, "record", boom)

    response = client.post(
        f"/v1/connections/{connection_id}/sync",
        headers={CSRF_HEADER_NAME: csrf, "Idempotency-Key": "k1"},
    )

    assert response.status_code == 200
    assert response.json()["created"] is True
    with factory() as session:
        assert session.scalars(
            select(Job).where(Job.job_type == "connection.incremental_sync")
        ).one()


def test_a_failed_link_audit_does_not_fail_the_link(
    client: TestClient, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The connection is stored before the audit is appended.

    Failing the callback here would tell a user their link failed while the
    grant is already sealed and usable — the worst possible answer, since a
    retry would re-run an OAuth exchange whose code is now spent.
    """
    _login(client, factory)

    def boom(*_args: object, **_kwargs: object) -> None:
        msg = "audit store unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(AuditRepository, "record", boom)
    connection_id = _link_polar(client)

    with factory() as session:
        connection = session.get(Connection, connection_id)
    assert connection is not None
    assert connection.status == "active"
