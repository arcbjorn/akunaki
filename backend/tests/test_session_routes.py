"""Cookie authentication and CSRF enforcement over HTTP.

These cover the boundary a browser actually hits: cookie attributes, the
generic 401, CSRF on mutations, and the tenant coming from the session rather
than from a client-supplied parameter.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi import Response as FastAPIResponse
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import Tenant, User
from akunaki.adapters.db.session_repository import SessionRepository
from akunaki.api.app import create_app
from akunaki.api.security import (
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    clear_session_cookie,
    set_session_cookie,
)
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339
from akunaki.domain.sessions import IssuedSession

T0 = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def route_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "routes.db"
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
def factory(route_db: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=route_db))
    session_factory = create_session_factory(engine)
    with session_factory() as session, session.begin():
        for tenant_id, user_id, subject in (
            ("tenant-1", "user-1", "subject-1"),
            ("tenant-2", "user-2", "subject-2"),
        ):
            session.add(
                Tenant(
                    id=tenant_id,
                    created_at=NOW_S,
                    status="active",
                    primary_timezone="UTC",
                    display_name="Test",
                )
            )
            session.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    oidc_issuer="https://idp.example.com",
                    oidc_subject=subject,
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


def _with_cookie(client: TestClient, token: str) -> TestClient:
    """Attach a session cookie to the client.

    Per-request ``cookies=`` is deprecated in httpx2 because persistence
    semantics are ambiguous, so set it on the client instance.
    """
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, token)
    return client


def _issue(
    factory: sessionmaker[Session],
    *,
    user_id: str = "user-1",
    session_id: str = "sess-1",
    ttl: timedelta = timedelta(hours=12),
    now: datetime | None = None,
) -> IssuedSession:
    return SessionRepository(factory).issue(
        session_id=session_id,
        user_id=user_id,
        now=now or datetime.now(UTC),
        ttl=ttl,
    )


# ---------------------------------------------------------------------------
# Cookie attributes
# ---------------------------------------------------------------------------


def test_session_cookie_carries_required_attributes() -> None:
    """HttpOnly, Secure, and SameSite are the design's stated requirements."""
    app = FastAPI()

    @app.get("/set")
    def _set(reply: FastAPIResponse) -> dict[str, str]:
        set_session_cookie(reply, token="aks_test", max_age_seconds=3600)
        return {"ok": "1"}

    raw = TestClient(app).get("/set").headers["set-cookie"]
    cookie = SimpleCookie()
    cookie.load(raw)
    morsel = cookie[SESSION_COOKIE_NAME]

    assert morsel.value == "aks_test"
    assert morsel["httponly"] is True
    assert morsel["secure"] is True
    assert morsel["samesite"].lower() == "lax"
    assert morsel["path"] == "/"
    assert morsel["max-age"] == "3600"


def test_cleared_cookie_matches_the_attributes_it_was_set_with() -> None:
    """Mismatched attributes can leave the original cookie alive."""
    app = FastAPI()

    @app.get("/clear")
    def _clear(reply: FastAPIResponse) -> dict[str, str]:
        clear_session_cookie(reply)
        return {"ok": "1"}

    raw = TestClient(app).get("/clear").headers["set-cookie"]
    cookie = SimpleCookie()
    cookie.load(raw)
    morsel = cookie[SESSION_COOKIE_NAME]

    assert morsel["httponly"] is True
    assert morsel["secure"] is True
    assert morsel["samesite"].lower() == "lax"
    assert morsel["path"] == "/"


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_no_cookie_is_unauthenticated(client: TestClient) -> None:
    response = client.get("/v1/session")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "unauthenticated"


def test_valid_cookie_authenticates(factory: sessionmaker[Session], client: TestClient) -> None:
    issued = _issue(factory)
    response = _with_cookie(client, issued.token).get("/v1/session")

    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == "tenant-1"
    assert body["user_id"] == "user-1"


def test_unknown_expired_and_revoked_are_indistinguishable(
    factory: sessionmaker[Session], client: TestClient
) -> None:
    """Distinguishing them would help an attacker enumerate valid tokens."""
    expired = _issue(
        factory,
        session_id="sess-expired",
        ttl=timedelta(seconds=1),
        now=datetime.now(UTC) - timedelta(hours=2),
    )
    revoked = _issue(factory, session_id="sess-revoked")
    SessionRepository(factory).revoke(session_id="sess-revoked", now=datetime.now(UTC))

    bodies = []
    for token in ("aks_unknown", expired.token, revoked.token):
        response = _with_cookie(client, token).get("/v1/session")
        assert response.status_code == 401
        bodies.append(response.json())

    assert bodies[0] == bodies[1] == bodies[2]


def test_response_carries_no_secret_material(
    factory: sessionmaker[Session], client: TestClient
) -> None:
    issued = _issue(factory)
    body = _with_cookie(client, issued.token).get("/v1/session").text

    assert issued.token not in body
    assert issued.csrf_secret not in body


def test_session_responses_are_not_cached(
    factory: sessionmaker[Session], client: TestClient
) -> None:
    issued = _issue(factory)
    response = _with_cookie(client, issued.token).get("/v1/session")
    assert response.headers["cache-control"] == "private, no-store"


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def test_mutation_without_csrf_header_is_forbidden(
    factory: sessionmaker[Session], client: TestClient
) -> None:
    """A cookie alone must not authorize a state-changing request."""
    issued = _issue(factory)
    response = _with_cookie(client, issued.token).post("/v1/session/logout")

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "forbidden"


def test_mutation_with_wrong_csrf_secret_is_forbidden(
    factory: sessionmaker[Session], client: TestClient
) -> None:
    issued = _issue(factory)
    response = _with_cookie(client, issued.token).post(
        "/v1/session/logout", headers={CSRF_HEADER_NAME: "not-the-secret"}
    )
    assert response.status_code == 403


def test_another_sessions_csrf_secret_is_forbidden(
    factory: sessionmaker[Session], client: TestClient
) -> None:
    """The CSRF secret must be bound to the session presenting it."""
    mine = _issue(factory, session_id="sess-mine")
    theirs = _issue(factory, user_id="user-2", session_id="sess-theirs")

    response = _with_cookie(client, mine.token).post(
        "/v1/session/logout", headers={CSRF_HEADER_NAME: theirs.csrf_secret}
    )
    assert response.status_code == 403


def test_mutation_with_valid_csrf_succeeds(
    factory: sessionmaker[Session], client: TestClient
) -> None:
    issued = _issue(factory)
    response = _with_cookie(client, issued.token).post(
        "/v1/session/logout", headers={CSRF_HEADER_NAME: issued.csrf_secret}
    )

    assert response.status_code == 200
    assert response.json()["revoked"] is True


def test_safe_methods_need_no_csrf(factory: sessionmaker[Session], client: TestClient) -> None:
    issued = _issue(factory)
    assert _with_cookie(client, issued.token).get("/v1/session").status_code == 200


def test_csrf_is_checked_only_after_authentication(client: TestClient) -> None:
    """An unauthenticated mutation is 401, not 403."""
    response = client.post("/v1/session/logout", headers={CSRF_HEADER_NAME: "anything"})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


def test_logout_revokes_server_side_and_clears_the_cookie(
    factory: sessionmaker[Session], client: TestClient
) -> None:
    """Clearing the cookie alone would leave a captured token usable."""
    issued = _issue(factory)
    response = _with_cookie(client, issued.token).post(
        "/v1/session/logout", headers={CSRF_HEADER_NAME: issued.csrf_secret}
    )

    assert response.status_code == 200
    assert SESSION_COOKIE_NAME in response.headers["set-cookie"]

    # The token is dead server-side even if a copy was captured.
    replay = _with_cookie(client, issued.token).get("/v1/session")
    assert replay.status_code == 401


def test_second_logout_reports_already_revoked(
    factory: sessionmaker[Session], client: TestClient
) -> None:
    issued = _issue(factory)
    headers = {CSRF_HEADER_NAME: issued.csrf_secret}
    authed = _with_cookie(client, issued.token)

    assert authed.post("/v1/session/logout", headers=headers).json()["revoked"] is True
    # The now-revoked session cannot authenticate a second logout.
    assert authed.post("/v1/session/logout", headers=headers).status_code == 401


def test_logout_everywhere_revokes_all_user_sessions(
    factory: sessionmaker[Session], client: TestClient
) -> None:
    """One call ends every session the user holds, not just this browser's."""
    first = _issue(factory, session_id="sess-a")
    _issue(factory, session_id="sess-b")  # a second live session for the same user
    _issue(factory, user_id="user-2", session_id="sess-other")  # another user's

    response = _with_cookie(client, first.token).post(
        "/v1/session/logout-everywhere", headers={CSRF_HEADER_NAME: first.csrf_secret}
    )
    assert response.status_code == 200
    assert response.json()["revoked_count"] == 2  # both of user-1's, not user-2's
    assert SESSION_COOKIE_NAME in response.headers["set-cookie"]

    # Both of the user's sessions are dead; the other user's is untouched.
    assert SessionRepository(factory).validate(token=first.token, now=datetime.now(UTC)).ok is False


def test_logout_everywhere_requires_csrf(
    factory: sessionmaker[Session], client: TestClient
) -> None:
    issued = _issue(factory)
    response = _with_cookie(client, issued.token).post("/v1/session/logout-everywhere")
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Tenant scoping
# ---------------------------------------------------------------------------


def test_tenant_comes_from_the_session_not_the_request(
    factory: sessionmaker[Session], client: TestClient
) -> None:
    """A caller must not be able to select a tenant by parameter."""
    mine = _issue(factory, session_id="sess-mine")

    response = _with_cookie(client, mine.token).get(
        "/v1/session",
        params={"tenant_id": "tenant-2"},
    )

    assert response.status_code == 200
    assert response.json()["tenant_id"] == "tenant-1"


def test_each_session_sees_only_its_own_tenant(
    factory: sessionmaker[Session], client: TestClient
) -> None:
    first = _issue(factory, user_id="user-1", session_id="s1")
    second = _issue(factory, user_id="user-2", session_id="s2")

    assert _with_cookie(client, first.token).get("/v1/session").json()["tenant_id"] == "tenant-1"
    assert _with_cookie(client, second.token).get("/v1/session").json()["tenant_id"] == "tenant-2"
