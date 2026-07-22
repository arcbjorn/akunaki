"""End-to-end coverage of ``GET /v1/provenance/{token}`` over real HTTP.

Verify an authenticated caller can resolve their own opaque token to disclosed
lineage (versions, status, freshness) without any id leaking, that an unknown
token and a cross-tenant token both 404 (indistinguishable), and that the route
requires a session.
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
from akunaki.adapters.db.models import DerivationRun, Tenant, User
from akunaki.adapters.db.session_repository import SessionRepository
from akunaki.api.app import create_app
from akunaki.api.security import SESSION_COOKIE_NAME
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
DAY = "2026-07-20"
TOKEN = "opaque_tok_route"


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def route_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "provenance_routes.db"
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
                    display_name=tenant_id,
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


def _seed_run(factory: sessionmaker[Session], *, tenant_id: str, token: str) -> None:
    with factory() as session, session.begin():
        session.add(
            DerivationRun(
                id=f"run-{tenant_id}",
                tenant_id=tenant_id,
                artifact_kind="score",
                local_health_day=DAY,
                formula_version="general_recovery_v0.1.0",
                dependency_hash="",
                confidence=0.9,
                freshness_at=NOW_S,
                as_of_at=None,
                status="ok",
                provenance_token=token,
                superseded_by=None,
                created_at=NOW_S,
            )
        )


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


def test_resolve_returns_disclosed_lineage(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    _seed_run(factory, tenant_id="tenant-1", token=TOKEN)
    _login(client, factory)

    resp = client.get(f"/v1/provenance/{TOKEN}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["artifact_kind"] == "score"
    assert body["local_health_day"] == DAY
    assert body["formula_version"] == "general_recovery_v0.1.0"
    assert body["status"] == "ok"
    assert body["inputs"] == []
    # No id of any stored row is ever disclosed.
    assert "id" not in body
    assert "run_id" not in body


def test_unknown_token_is_404(client: TestClient, factory: sessionmaker[Session]) -> None:
    _seed_run(factory, tenant_id="tenant-1", token=TOKEN)
    _login(client, factory)

    resp = client.get("/v1/provenance/opaque_tok_absent")
    assert resp.status_code == 404


def test_cross_tenant_token_is_404(client: TestClient, factory: sessionmaker[Session]) -> None:
    # A real token owned by tenant-2, presented by tenant-1's session, is
    # indistinguishable from an unknown one: 404, never a leak.
    _seed_run(factory, tenant_id="tenant-2", token="opaque_tok_t2")
    _login(client, factory, user_id="user-1")

    resp = client.get("/v1/provenance/opaque_tok_t2")
    assert resp.status_code == 404


def test_requires_a_session(client: TestClient, factory: sessionmaker[Session]) -> None:
    _seed_run(factory, tenant_id="tenant-1", token=TOKEN)
    client.cookies.clear()

    resp = client.get(f"/v1/provenance/{TOKEN}")
    assert resp.status_code == 401
