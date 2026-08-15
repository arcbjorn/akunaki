"""Service tokens: hash-only persistence and the bearer path over ``/v1/tools``.

The repository half proves the credential lifecycle (issue once, validate,
expire, revoke) and that the raw token never lands in the database. The route
half proves the one place Bearer is accepted: the tools surface — reads work
without a cookie or CSRF, mutations are refused outright, and every other
``/v1`` route still demands a session.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import FactRecord, ServiceToken, SleepSession, Tenant, User
from akunaki.adapters.db.service_token_repository import ServiceTokenRepository
from akunaki.api.app import create_app
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339
from akunaki.domain.service_tokens import ServiceTokenRejection, ServiceTokenScope
from conftest import upgrade_to_head

T0 = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
DAY = "2026-08-15"


@pytest.fixture
def route_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "service_tokens.db"
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
    return TestClient(create_app(Settings(database_url=route_db)))


def _issue(factory: sessionmaker[Session], *, ttl: timedelta | None = None) -> str:
    issued = ServiceTokenRepository(factory).issue(
        token_id="tok-1", user_id="user-1", name="odin-personal", now=T0, ttl=ttl
    )
    return issued.token


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Repository lifecycle
# ---------------------------------------------------------------------------


def test_issue_stores_the_hash_never_the_token(factory: sessionmaker[Session]) -> None:
    token = _issue(factory)
    assert token.startswith("aksvc_")
    with factory() as session:
        row = session.scalars(select(ServiceToken)).one()
    assert row.token_hash != token
    assert token not in (row.token_hash, row.id, row.name)
    assert row.scope == "read"
    assert row.expires_at is None


def test_validate_accepts_a_live_token(factory: sessionmaker[Session]) -> None:
    token = _issue(factory)
    result = ServiceTokenRepository(factory).validate(token=token, now=T0)
    assert result.ok and result.principal is not None
    assert result.principal.tenant_id == "tenant-1"
    assert result.principal.user_id == "user-1"
    assert result.principal.scope is ServiceTokenScope.READ


def test_validate_rejects_unknown_expired_and_revoked(factory: sessionmaker[Session]) -> None:
    repo = ServiceTokenRepository(factory)

    assert repo.validate(token="aksvc_nope", now=T0).rejection is ServiceTokenRejection.NOT_FOUND

    expiring = _issue(factory, ttl=timedelta(hours=1))
    late = T0 + timedelta(hours=2)
    assert repo.validate(token=expiring, now=late).rejection is ServiceTokenRejection.EXPIRED

    assert repo.revoke(token_id="tok-1", now=T0) is True
    assert repo.validate(token=expiring, now=T0).rejection is ServiceTokenRejection.REVOKED
    # Revoking again is a no-op, not an error.
    assert repo.revoke(token_id="tok-1", now=T0) is False


def test_issue_validates_its_inputs(factory: sessionmaker[Session]) -> None:
    repo = ServiceTokenRepository(factory)
    with pytest.raises(ValueError, match="must be non-empty"):
        repo.issue(token_id="", user_id="user-1", name="x", now=T0)
    with pytest.raises(ValueError, match="at least one second"):
        repo.issue(token_id="t", user_id="user-1", name="x", now=T0, ttl=timedelta(0))
    with pytest.raises(ValueError, match="not found"):
        repo.issue(token_id="t", user_id="nobody", name="x", now=T0)


def test_list_for_tenant_names_tokens_without_secrets(factory: sessionmaker[Session]) -> None:
    _issue(factory)
    rows = ServiceTokenRepository(factory).list_for_tenant(tenant_id="tenant-1")
    assert rows == [("tok-1", "odin-personal", "read", None)]


# ---------------------------------------------------------------------------
# Bearer over /v1/tools
# ---------------------------------------------------------------------------


def _seed_sleep(factory: sessionmaker[Session], *, day: str, fact_id: str) -> None:
    with factory() as session, session.begin():
        session.add(
            FactRecord(
                id=fact_id,
                tenant_id="tenant-1",
                connection_id=None,
                provider="oura",
                entity_type="sleep_session",
                vendor_record_id=fact_id,
                origin=None,
                method="wearable",
                utc_instant=NOW_S,
                start_utc=NOW_S,
                end_utc=NOW_S,
                source_offset_minutes=0,
                iana_timezone="UTC",
                local_health_day=day,
                unit=None,
                quality="high",
                confidence=1.0,
                freshness_at=NOW_S,
                raw_revision_id=None,
                raw_payload_id=None,
                schema_version="v1",
                normalizer_version="sleep_v0.1.0",
                content_hash=fact_id,
                fact_key=f"sleep_session:{fact_id}",
                version_n=1,
                is_current=1,
                superseded_by=None,
                superseded_at=None,
                deletion_state="active",
                exclude_from_load=0,
                created_at=NOW_S,
            )
        )
        session.add(
            SleepSession(
                fact_record_id=fact_id,
                tenant_id="tenant-1",
                is_nap=0,
                duration_min=420.0,
                time_in_bed_min=None,
                efficiency_pct=None,
                light_min=None,
                deep_min=None,
                rem_min=None,
                awake_min=None,
            )
        )


def test_bearer_lists_tools_without_a_cookie(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    token = _issue(factory)
    response = client.get("/v1/tools", headers=_bearer(token))
    assert response.status_code == 200
    names = {t["name"] for t in response.json()["tools"]}
    assert "health.get_today" in names


def test_bearer_invokes_a_read_tool_without_csrf(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    _seed_sleep(factory, day=DAY, fact_id="s1")
    token = _issue(factory)
    response = client.post(
        "/v1/tools/health.get_sleep", headers=_bearer(token), json={"input": {"day": DAY}}
    )
    assert response.status_code == 200
    assert response.json()["local_health_day"] == DAY


def test_bearer_cannot_invoke_a_mutating_tool(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    # connections.sync mutates (side_effect != none); a read-scoped token is
    # refused before the confirmation machinery, with the generic 403.
    token = _issue(factory)
    response = client.post(
        "/v1/tools/connections.sync", headers=_bearer(token), json={"input": {"connection_id": "c"}}
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "forbidden"


def test_bearer_rejections_are_one_generic_401(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    expiring = _issue(factory, ttl=timedelta(hours=1))
    ServiceTokenRepository(factory).revoke(token_id="tok-1", now=datetime.now(UTC))

    for headers in (
        _bearer("aksvc_unknown"),
        _bearer(expiring),  # revoked
        {"Authorization": "Basic dXNlcjpwdw=="},  # wrong scheme
        {"Authorization": "Bearer "},  # empty credential
    ):
        assert client.get("/v1/tools", headers=headers).status_code == 401


def test_a_bearer_header_is_never_downgraded_to_the_cookie(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    # A valid cookie session plus a bad bearer token must fail: the explicit
    # credential decides, or a stolen header could ride ambient cookie auth.
    from akunaki.adapters.db.session_repository import SessionRepository
    from akunaki.api.security import SESSION_COOKIE_NAME

    issued = SessionRepository(factory).issue(
        session_id="sess-1", user_id="user-1", now=datetime.now(UTC), ttl=timedelta(hours=1)
    )
    client.cookies.set(SESSION_COOKIE_NAME, issued.token)
    assert client.get("/v1/tools", headers=_bearer("aksvc_bad")).status_code == 401


def test_bearer_does_not_open_the_rest_of_v1(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    # The token is scoped to the tools surface; the product routes stay
    # session-only until Bearer is deliberately extended to them.
    token = _issue(factory)
    assert client.get("/v1/today", headers=_bearer(token)).status_code == 401
    assert client.get("/v1/recovery", headers=_bearer(token)).status_code == 401
