"""``GET /v1/public/training``: the unauthenticated 30-day training calendar.

The one surface that answers anyone about one operator-named tenant, so the
tests are mostly about what it does **not** do: mount uninvited, take a
session, name a day, or leak a measurement.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.fact_repository import FactRepository
from akunaki.adapters.db.models import Tenant, User
from akunaki.api.app import create_app
from akunaki.application.public_training_surface import WINDOW_DAYS
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339
from akunaki.domain.workout_normalizer import WorkoutFact
from conftest import upgrade_to_head

T0 = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)


def _today() -> str:
    """The seeded tenant is in UTC, so today is the UTC date."""
    return datetime.now(UTC).date().isoformat()


def _days_ago(offset: int) -> str:
    return (datetime.now(UTC).date() - timedelta(days=offset)).isoformat()


@pytest.fixture
def route_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "public_training.db"
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
    for tenant_id, user_id, subject in (
        ("tenant-1", "user-1", "subject-1"),
        ("tenant-2", "user-2", "subject-2"),
    ):
        with session_factory() as session, session.begin():
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
    return TestClient(
        create_app(Settings(database_url=route_db, public_training_tenant_id="tenant-1"))
    )


def _seed_workout(
    factory: sessionmaker[Session],
    *,
    workout_id: str,
    day: str,
    tenant_id: str = "tenant-1",
    provider: str = "polar",
) -> None:
    """Write one workout through the real fact-write path."""
    FactRepository(factory).write_workout_fact(
        fact_record_id=workout_id,
        tenant_id=tenant_id,
        connection_id=None,
        fact=WorkoutFact(
            vendor_record_id=workout_id,
            start_utc=f"{day}T07:00:00Z",
            end_utc=f"{day}T08:00:00Z",
            local_health_day=day,
            source_offset_minutes=0,
            session_load=100.0,
            zone1_min=10.0,
            zone2_min=20.0,
            zone3_min=15.0,
            zone4_min=5.0,
            zone5_min=0.0,
            quality="high",
            confidence=1.0,
            content_hash=workout_id,
        ),
        raw_revision_id=None,
        raw_payload_id=None,
        schema_version=f"{provider}.v1",
        now=T0,
    )


# --- mounting ---------------------------------------------------------------


def test_absent_unless_a_tenant_is_named() -> None:
    """An unauthenticated surface is not registered on an opted-out deployment."""
    app = create_app(Settings(database_url="sqlite+libsql://"))
    with TestClient(app) as client:
        assert client.get("/v1/public/training").status_code == 404
    assert "/v1/public/training" not in app.openapi()["paths"]


def test_needs_no_session(client: TestClient, factory: sessionmaker[Session]) -> None:
    client.cookies.clear()
    assert client.get("/v1/public/training").status_code == 200


def test_unknown_tenant_is_unavailable_not_empty(route_db: str) -> None:
    """A misnamed tenant must not render as a person who never trains."""
    client = TestClient(
        create_app(Settings(database_url=route_db, public_training_tenant_id="nobody"))
    )
    response = client.get("/v1/public/training")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "public_training_unavailable"


# --- shape ------------------------------------------------------------------


def test_window_is_thirty_days_ending_today_oldest_first(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    body = client.get("/v1/public/training").json()
    assert body["window_days"] == WINDOW_DAYS == 30
    assert body["as_of"] == _today()
    days = [entry["day"] for entry in body["days"]]
    assert len(days) == 30
    assert days[0] == _days_ago(29)
    assert days[-1] == _today()
    assert days == sorted(days)


def test_no_workouts_is_all_untrained(client: TestClient, factory: sessionmaker[Session]) -> None:
    body = client.get("/v1/public/training").json()
    assert all(entry["trained"] is False for entry in body["days"])
    assert body["sources"] == []


def test_trained_days(client: TestClient, factory: sessionmaker[Session]) -> None:
    for offset in (0, 1, 2, 10, 11, 12, 13, 14):
        _seed_workout(factory, workout_id=f"w-{offset}", day=_days_ago(offset))
    # Two sessions on one day still read as one trained day.
    _seed_workout(factory, workout_id="w-10-second", day=_days_ago(10))

    body = client.get("/v1/public/training").json()
    trained = {entry["day"] for entry in body["days"] if entry["trained"]}
    assert trained == {_days_ago(o) for o in (0, 1, 2, 10, 11, 12, 13, 14)}
    assert body["sources"] == ["polar"]


def test_a_day_outside_the_window_is_not_shown(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    _seed_workout(factory, workout_id="w-old", day=_days_ago(30))
    body = client.get("/v1/public/training").json()
    assert not any(entry["trained"] for entry in body["days"])
    assert _days_ago(30) not in {entry["day"] for entry in body["days"]}


def test_other_tenants_sessions_do_not_count(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    _seed_workout(factory, workout_id="w-other", day=_today(), tenant_id="tenant-2")
    body = client.get("/v1/public/training").json()
    assert not any(entry["trained"] for entry in body["days"])


# --- disclosure -------------------------------------------------------------


def test_discloses_no_measurement(client: TestClient, factory: sessionmaker[Session]) -> None:
    """Per day: the day and a boolean. Nothing a session could be rebuilt from."""
    _seed_workout(factory, workout_id="w-0", day=_today())
    body = client.get("/v1/public/training").json()
    assert set(body) == {"as_of", "window_days", "days", "sources", "definition"}
    assert all(set(entry) == {"day", "trained"} for entry in body["days"])
    text = client.get("/v1/public/training").text
    for leak in ("start", "end", "zone", "load", "utc", "min", "score"):
        assert leak not in text.lower()


def test_is_publicly_cacheable_and_cross_origin_readable(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    response = client.get("/v1/public/training")
    assert response.headers["cache-control"] == "public, max-age=3600"
    assert response.headers["access-control-allow-origin"] == "*"
    # Relaxed for this route only; the security middleware default is same-origin.
    assert response.headers["cross-origin-resource-policy"] == "cross-origin"
