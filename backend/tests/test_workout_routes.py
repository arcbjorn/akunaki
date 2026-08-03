"""``GET /v1/workouts`` list and detail, over real HTTP.

Workout facts have been written since the Polar connector shipped, but the only
read path was the daily load aggregate feeding ACWR. These drive the surface
that makes the sessions themselves visible.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.fact_repository import FactRepository
from akunaki.adapters.db.models import FactRecord, Tenant, User
from akunaki.adapters.db.session_repository import SessionRepository
from akunaki.api.app import create_app
from akunaki.api.security import SESSION_COOKIE_NAME
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339
from akunaki.domain.workout_normalizer import WorkoutFact

T0 = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
DAY = "2026-07-25"


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def route_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "workout_routes.db"
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


def _seed_workout(
    factory: sessionmaker[Session],
    *,
    workout_id: str,
    start_hour: int,
    tenant_id: str = "tenant-1",
    day: str = DAY,
    session_load: float = 100.0,
) -> None:
    """Write one workout through the real fact-write path."""
    start = f"{day}T{start_hour:02d}:00:00Z"
    end = f"{day}T{start_hour + 1:02d}:00:00Z"
    FactRepository(factory).write_workout_fact(
        fact_record_id=workout_id,
        tenant_id=tenant_id,
        connection_id=None,
        fact=WorkoutFact(
            vendor_record_id=workout_id,
            start_utc=start,
            end_utc=end,
            local_health_day=day,
            source_offset_minutes=0,
            session_load=session_load,
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
        schema_version="polar.v1",
        now=T0,
    )


def test_requires_a_session() -> None:
    client = TestClient(create_app(Settings(database_url="sqlite+libsql:///:memory:")))
    assert client.get("/v1/workouts").status_code == 401


def test_no_workouts_is_an_empty_page(client: TestClient, factory: sessionmaker[Session]) -> None:
    """A user with no workout connector has no sessions — not an error."""
    _login(client, factory)
    response = client.get("/v1/workouts")

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


def test_list_discloses_zone_minutes_and_load(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    _login(client, factory)
    _seed_workout(factory, workout_id="w1", start_hour=6)

    [row] = client.get("/v1/workouts").json()["items"]

    assert row["workout_id"] == "w1"
    assert row["provider"] == "polar"
    assert row["local_health_day"] == DAY
    assert row["session_load"] == 100.0
    assert row["zone2_min"] == 20.0
    # The disclosed total is the sum of the five zones, not a vendor field.
    assert row["total_zone_min"] == 50.0


def test_list_is_newest_first(client: TestClient, factory: sessionmaker[Session]) -> None:
    _login(client, factory)
    _seed_workout(factory, workout_id="early", start_hour=6)
    _seed_workout(factory, workout_id="late", start_hour=18)

    items = client.get("/v1/workouts").json()["items"]

    assert [i["workout_id"] for i in items] == ["late", "early"]


def test_cursor_walks_every_workout_exactly_once(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """The pagination contract: no row skipped, none repeated."""
    _login(client, factory)
    for hour in range(5):
        _seed_workout(factory, workout_id=f"w{hour}", start_hour=hour + 6)

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):  # bounded: 5 rows at 2 per page needs 3 requests
        params = {"limit": 2} if cursor is None else {"limit": 2, "cursor": cursor}
        body = client.get("/v1/workouts", params=params).json()
        seen.extend(i["workout_id"] for i in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert seen == ["w4", "w3", "w2", "w1", "w0"]
    assert len(set(seen)) == 5


def test_last_page_has_no_next_cursor(client: TestClient, factory: sessionmaker[Session]) -> None:
    """An exactly-full final page must not advertise a page that isn't there."""
    _login(client, factory)
    _seed_workout(factory, workout_id="w1", start_hour=6)
    _seed_workout(factory, workout_id="w2", start_hour=7)

    body = client.get("/v1/workouts", params={"limit": 2}).json()

    assert len(body["items"]) == 2
    assert body["next_cursor"] is None


def test_malformed_cursor_is_422(client: TestClient, factory: sessionmaker[Session]) -> None:
    """A cursor we never issued is a client error, not a silent restart."""
    _login(client, factory)
    _seed_workout(factory, workout_id="w1", start_hour=6)

    assert client.get("/v1/workouts", params={"cursor": "not-a-cursor"}).status_code == 422


def test_duplicate_from_a_second_provider_is_hidden(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """``exclude_from_load`` marks a duplicate of the same real session.

    Showing it would present one workout twice, which is worse than the load
    double-count the flag exists to prevent.
    """
    _login(client, factory)
    _seed_workout(factory, workout_id="polar-1", start_hour=6)
    _seed_workout(factory, workout_id="google-copy", start_hour=6)
    with factory() as session, session.begin():
        session.execute(
            update(FactRecord).where(FactRecord.id == "google-copy").values(exclude_from_load=1)
        )

    items = client.get("/v1/workouts").json()["items"]

    assert [i["workout_id"] for i in items] == ["polar-1"]


def test_list_never_serves_another_tenant(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    _login(client, factory)
    _seed_workout(factory, workout_id="theirs", start_hour=6, tenant_id="tenant-2")

    assert client.get("/v1/workouts").json()["items"] == []


def test_detail_returns_one_workout(client: TestClient, factory: sessionmaker[Session]) -> None:
    _login(client, factory)
    _seed_workout(factory, workout_id="w1", start_hour=6, session_load=250.0)

    body = client.get("/v1/workouts/w1").json()

    assert body["workout_id"] == "w1"
    assert body["session_load"] == 250.0


def test_unknown_workout_is_404(client: TestClient, factory: sessionmaker[Session]) -> None:
    _login(client, factory)
    assert client.get("/v1/workouts/nope").status_code == 404


def test_another_tenants_workout_is_the_same_404(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """Unknown and cross-tenant must be indistinguishable, so ids cannot be probed."""
    _login(client, factory)
    _seed_workout(factory, workout_id="theirs", start_hour=6, tenant_id="tenant-2")

    theirs = client.get("/v1/workouts/theirs")
    unknown = client.get("/v1/workouts/nope")

    assert theirs.status_code == unknown.status_code == 404
    assert theirs.json() == unknown.json()


def test_cursor_handles_workouts_starting_at_the_same_instant(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """Two sessions can share a start time; paging must not lose one.

    Without an id tie-breaker in the keyset predicate, "older than the last
    row's start" skips every other row sharing that exact timestamp.
    """
    _login(client, factory)
    for suffix in ("a", "b", "c"):
        _seed_workout(factory, workout_id=f"same-{suffix}", start_hour=6)

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        params = {"limit": 1} if cursor is None else {"limit": 1, "cursor": cursor}
        body = client.get("/v1/workouts", params=params).json()
        seen.extend(i["workout_id"] for i in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert sorted(seen) == ["same-a", "same-b", "same-c"]
