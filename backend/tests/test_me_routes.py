"""``GET /v1/me`` over real HTTP.

The load-bearing properties: the tenant's ``primary_timezone`` is finally
readable (a client cannot otherwise know which timezone its ``day`` parameters
are in), the OIDC subject is never echoed, and a session for one tenant can
never read another's account.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import Tenant, User
from akunaki.adapters.db.session_repository import SessionRepository
from akunaki.api.app import create_app
from akunaki.api.security import SESSION_COOKIE_NAME
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339

NOW_S = to_utc_rfc3339(datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC))
SUBJECT = "subject-must-never-be-echoed"


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def route_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "me_routes.db"
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
                primary_timezone="Europe/Berlin",
                display_name="Test Person",
            )
        )
        session.add(
            User(
                id="user-1",
                tenant_id="tenant-1",
                oidc_issuer="https://idp.example.com",
                oidc_subject=SUBJECT,
                email="person@example.com",
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


def _login(
    client: TestClient,
    factory: sessionmaker[Session],
    *,
    user_id: str = "user-1",
    session_id: str = "sess-user-1",
) -> None:
    issued = SessionRepository(factory).issue(
        session_id=session_id,
        user_id=user_id,
        now=datetime.now(UTC),
        ttl=timedelta(hours=12),
    )
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, issued.token)


def test_requires_a_session() -> None:
    client = TestClient(create_app(Settings(database_url="sqlite+libsql:///:memory:")))

    assert client.get("/v1/me").status_code == 401


def test_returns_the_callers_account(client: TestClient, factory: sessionmaker[Session]) -> None:
    _login(client, factory)

    body = client.get("/v1/me").json()

    assert body["user_id"] == "user-1"
    assert body["email"] == "person@example.com"
    assert body["created_at"] == NOW_S


def test_discloses_the_tenant_timezone(client: TestClient, factory: sessionmaker[Session]) -> None:
    """The reason this endpoint earns its place.

    Every day surface requires an explicit ``day`` because the server must not
    guess a local health day — but until now the client had no way to learn
    which timezone defines that day either.
    """
    _login(client, factory)

    body = client.get("/v1/me").json()

    assert body["tenant"]["primary_timezone"] == "Europe/Berlin"


def test_carries_the_tenant_block(client: TestClient, factory: sessionmaker[Session]) -> None:
    _login(client, factory)

    tenant = client.get("/v1/me").json()["tenant"]

    assert tenant["tenant_id"] == "tenant-1"
    assert tenant["status"] == "active"
    assert tenant["display_name"] == "Test Person"


def test_never_echoes_the_oidc_subject(client: TestClient, factory: sessionmaker[Session]) -> None:
    """The subject is the identity credential login matches on.

    Echoing it to a browser puts an account-linking key somewhere it is never
    needed.
    """
    _login(client, factory)

    raw = client.get("/v1/me").text

    assert SUBJECT not in raw
    assert "oidc_subject" not in raw
    assert "idp.example.com" not in raw


def test_carries_no_session_material(client: TestClient, factory: sessionmaker[Session]) -> None:
    """``/v1/session`` owns session state; a token must never appear here."""
    _login(client, factory)

    body = client.get("/v1/me").json()

    assert set(body) == {"user_id", "email", "created_at", "tenant"}
    assert "session_id" not in body
    assert "token" not in client.get("/v1/me").text


def test_carries_no_health_values(client: TestClient, factory: sessionmaker[Session]) -> None:
    """An account surface, not a health one."""
    _login(client, factory)

    raw = client.get("/v1/me").text.lower()

    for token in ("hrv", "sleep", "recovery", "score", "steps"):
        assert token not in raw


def test_a_null_email_is_returned_as_null(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """An IdP that releases no email must not become an empty string."""
    with factory() as session, session.begin():
        session.add(
            Tenant(
                id="tenant-2",
                created_at=NOW_S,
                status="active",
                primary_timezone="UTC",
                display_name=None,
            )
        )
        session.add(
            User(
                id="user-2",
                tenant_id="tenant-2",
                oidc_issuer="https://idp.example.com",
                oidc_subject="subject-2",
                email=None,
                created_at=NOW_S,
            )
        )
    _login(client, factory, user_id="user-2", session_id="sess-user-2")

    body = client.get("/v1/me").json()

    assert body["email"] is None
    assert body["tenant"]["display_name"] is None


def test_reads_only_the_callers_own_tenant(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """A second tenant's account must be unreachable with this session."""
    with factory() as session, session.begin():
        session.add(
            Tenant(
                id="tenant-2",
                created_at=NOW_S,
                status="active",
                primary_timezone="Asia/Tokyo",
                display_name="Other Person",
            )
        )
        session.add(
            User(
                id="user-2",
                tenant_id="tenant-2",
                oidc_issuer="https://idp.example.com",
                oidc_subject="subject-2",
                email="other@example.com",
                created_at=NOW_S,
            )
        )
    _login(client, factory)

    body = client.get("/v1/me").json()

    assert body["user_id"] == "user-1"
    assert body["tenant"]["tenant_id"] == "tenant-1"
    assert body["tenant"]["primary_timezone"] == "Europe/Berlin"
    assert "other@example.com" not in client.get("/v1/me").text


def test_a_deleted_account_cannot_be_read(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """A session never outlives its account.

    ``sessions`` cascades from ``users`` (and from ``tenants``, which is what
    the privacy scrub deletes), so erasing the account destroys the cookie's
    session row with it. The result is a 401 at the auth boundary, never a
    readable account — which is why the route's ``account_is None`` branch is
    unreachable rather than merely untested.
    """
    _login(client, factory)
    with factory() as session, session.begin():
        session.execute(delete(User).where(User.id == "user-1"))

    assert client.get("/v1/me").status_code == 401
