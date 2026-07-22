"""End-to-end coverage of ``POST /v1/checkin`` over real HTTP.

The first authenticated write path: it requires a session cookie AND a CSRF
header (state-changing method), records a versioned check-in, and — once
recorded — makes the subjective component available to recovery scoring.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.checkin_repository import CheckInRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import SubjectiveCheckIn, Tenant, User
from akunaki.adapters.db.session_repository import SessionRepository
from akunaki.api.app import create_app
from akunaki.api.security import CSRF_HEADER_NAME, SESSION_COOKIE_NAME
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339
from akunaki.domain.subjective import SubjectiveInputs, subjective_component

T0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
DAY = "2026-07-22"


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def route_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "checkin_routes.db"
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


def _login(client: TestClient, factory: sessionmaker[Session]) -> str:
    issued = SessionRepository(factory).issue(
        session_id="sess-user-1",
        user_id="user-1",
        now=datetime.now(UTC),
        ttl=timedelta(hours=12),
    )
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, issued.token)
    return issued.csrf_secret


_BODY = {"local_health_day": DAY, "energy_n": 0.6, "stress_n": 0.4, "symptom_burden_n": 0.2}


def test_requires_a_session() -> None:
    client = TestClient(create_app(Settings(database_url="sqlite+libsql:///:memory:")))
    response = client.post("/v1/checkin", json=_BODY)
    assert response.status_code == 401


def test_requires_csrf_header(client: TestClient, factory: sessionmaker[Session]) -> None:
    _login(client, factory)  # cookie set, but no CSRF header supplied
    response = client.post("/v1/checkin", json=_BODY)
    assert response.status_code == 403


def test_records_a_check_in(client: TestClient, factory: sessionmaker[Session]) -> None:
    csrf = _login(client, factory)
    response = client.post("/v1/checkin", json=_BODY, headers={CSRF_HEADER_NAME: csrf})
    assert response.status_code == 200
    body = response.json()
    assert body["local_health_day"] == DAY
    assert body["version_n"] == 1

    inputs = CheckInRepository(factory).current_check_in_inputs(
        tenant_id="tenant-1", local_health_day=DAY
    )
    assert inputs is not None
    assert inputs.energy_n == 0.6


def test_resubmission_supersedes(client: TestClient, factory: sessionmaker[Session]) -> None:
    csrf = _login(client, factory)
    client.post("/v1/checkin", json=_BODY, headers={CSRF_HEADER_NAME: csrf})
    updated = {**_BODY, "energy_n": 0.9}
    second = client.post("/v1/checkin", json=updated, headers={CSRF_HEADER_NAME: csrf})
    assert second.json()["version_n"] == 2

    with factory() as session:
        current = session.scalars(
            select(SubjectiveCheckIn).where(SubjectiveCheckIn.is_current == 1)
        ).one()
    assert current.energy_n == 0.9


def test_out_of_range_value_is_rejected(client: TestClient, factory: sessionmaker[Session]) -> None:
    csrf = _login(client, factory)
    bad = {**_BODY, "energy_n": 1.5}
    response = client.post("/v1/checkin", json=bad, headers={CSRF_HEADER_NAME: csrf})
    assert response.status_code == 422


def test_recorded_check_in_feeds_recovery_subjective(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    # After recording, the day's subjective component is available: /v1/recovery
    # (computing on read, no stored score) exposes the subjective factor.
    csrf = _login(client, factory)
    client.post("/v1/checkin", json=_BODY, headers={CSRF_HEADER_NAME: csrf})

    body = client.get("/v1/recovery", params={"day": DAY}).json()
    factor_codes = {f["factor_code"] for f in body["factors"]}
    assert "subjective" in factor_codes
    # Sanity: the exposed magnitude matches the pure formula for these inputs.
    expected = subjective_component(
        SubjectiveInputs(energy_n=0.6, stress_n=0.4, symptom_burden_n=0.2)
    )
    subjective = next(f for f in body["factors"] if f["factor_code"] == "subjective")
    assert subjective["magnitude"] == pytest.approx(expected.c)
