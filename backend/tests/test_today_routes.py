"""End-to-end coverage of the composite ``/v1/today`` over real HTTP.

These verify the composite stitches recovery and sleep, discloses the unshipped
blocks as gaps, and never fabricates a score or a phantom sleep measurement.
Facts are seeded as ORM rows.
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
from akunaki.adapters.db.models import (
    DailyHealthScore,
    FactRecord,
    SleepSession,
    Tenant,
    User,
)
from akunaki.adapters.db.session_repository import SessionRepository
from akunaki.api.app import create_app
from akunaki.api.security import SESSION_COOKIE_NAME
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
TARGET_DAY = "2026-07-20"


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def route_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "today_routes.db"
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


def _seed_sleep(
    factory: sessionmaker[Session],
    *,
    day: str,
    duration_min: float,
    fact_id: str,
) -> None:
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
                duration_min=duration_min,
                time_in_bed_min=None,
                efficiency_pct=None,
                light_min=None,
                deep_min=None,
                rem_min=None,
                awake_min=None,
            )
        )


def _login(client: TestClient, factory: sessionmaker[Session]) -> None:
    issued = SessionRepository(factory).issue(
        session_id="sess-user-1",
        user_id="user-1",
        now=datetime.now(UTC),
        ttl=timedelta(hours=12),
    )
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, issued.token)


def _seed_score(
    factory: sessionmaker[Session],
    *,
    day: str,
    score: int,
    status: str = "partial",
    confidence: float = 0.7,
    available_weight: float = 0.60,
) -> None:
    with factory() as session, session.begin():
        session.add(
            DailyHealthScore(
                id=f"score-{day}",
                tenant_id="tenant-1",
                local_health_day=day,
                score_code="recovery",
                status=status,
                score=score,
                available_weight=available_weight,
                confidence=confidence,
                formula_version="general_recovery_v0.1.0",
                dependency_hash="seeded",
                freshness_at=NOW_S,
                as_of_at=NOW_S,
                version_n=2,
                is_current=1,
                superseded_by=None,
                superseded_at=None,
                created_at=NOW_S,
            )
        )


def _seed_high_anomaly(factory: sessionmaker[Session]) -> None:
    from akunaki.adapters.db.models import Anomaly as AnomalyRow

    with factory() as session, session.begin():
        session.add(
            AnomalyRow(
                id="an-1",
                tenant_id="tenant-1",
                feature_code="low_hrv",
                started_on=TARGET_DAY,
                ended_on=None,
                severity="high",
                z_like=-3.0,
                formula_version="anomaly_v0.1.0",
                is_active=1,
                consecutive_clear_days=0,
                created_at=NOW_S,
                updated_at=NOW_S,
            )
        )


def test_requires_a_session() -> None:
    client = TestClient(create_app(Settings(database_url="sqlite+libsql:///:memory:")))
    response = client.get("/v1/today", params={"day": TARGET_DAY})
    assert response.status_code == 401


def test_today_carries_served_score_freshness(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    _seed_score(factory, day=TARGET_DAY, score=77)
    _seed_sleep(factory, day=TARGET_DAY, duration_min=420.0, fact_id="today")
    _login(client, factory)

    body = client.get("/v1/today", params={"day": TARGET_DAY}).json()
    # The composite serves the stored recovery score and discloses its freshness.
    assert body["recovery"]["score"] == 77
    assert body["freshness_at"] == NOW_S


def test_composite_carries_recovery_and_sleep(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    _seed_sleep(factory, day=TARGET_DAY, duration_min=420.0, fact_id="today")
    _login(client, factory)

    response = client.get("/v1/today", params={"day": TARGET_DAY})
    assert response.status_code == 200
    body = response.json()

    # Recovery block: insufficient (no HRV/RHR), null score, but present.
    assert body["recovery"]["score_code"] == "recovery"
    assert body["recovery"]["status"] == "insufficient"
    assert body["recovery"]["score"] is None
    assert body["status"] == "insufficient"  # mirrors recovery

    # Sleep block: a real measurement, 420 of 480 -> 87.5% adherence.
    assert body["sleep"] is not None
    assert body["sleep"]["duration_min"] == 420.0
    assert body["sleep"]["adherence_pct"] == pytest.approx(87.5)

    assert body["formula_version"] == "general_recovery_v0.1.0"


def test_high_anomaly_floors_training_label_at_light(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    # A stored hard-band score (ok, high confidence) would be `hard`, but a
    # persisted high-severity anomaly floors it at light.
    _seed_score(factory, day=TARGET_DAY, score=90, status="ok", confidence=0.9)
    _seed_high_anomaly(factory)
    _login(client, factory)

    body = client.get("/v1/today", params={"day": TARGET_DAY}).json()
    assert body["recovery"]["score"] == 90
    assert body["training_recommendation"]["label"] == "light"


def test_no_anomaly_leaves_hard_label(client: TestClient, factory: sessionmaker[Session]) -> None:
    _seed_score(factory, day=TARGET_DAY, score=90, status="ok", confidence=0.9)
    _login(client, factory)
    body = client.get("/v1/today", params={"day": TARGET_DAY}).json()
    assert body["training_recommendation"]["label"] == "hard"


def test_training_label_and_recommendation_ship(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    # A sleep-only tenant: recovery insufficient -> training label insufficient,
    # and (with data gaps) the data-gap reconnect rule is the primary.
    _seed_sleep(factory, day=TARGET_DAY, duration_min=420.0, fact_id="today")
    _login(client, factory)

    body = client.get("/v1/today", params={"day": TARGET_DAY}).json()
    assert body["training_recommendation"]["label"] == "insufficient"
    assert body["training_recommendation"]["ruleset_version"] == "training_label_v0.1.0"
    # The recovery gate failed (missing HRV/RHR) -> a data gap -> reconnect rule.
    assert body["primary_recommendation"] is not None
    assert body["primary_recommendation"]["rule_id"] == "data_gap_reconnect"
    assert body["primary_recommendation"]["role"] == "primary"


def test_unshipped_blocks_are_disclosed_not_fabricated(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    _seed_sleep(factory, day=TARGET_DAY, duration_min=420.0, fact_id="today")
    _login(client, factory)
    body = client.get("/v1/today", params={"day": TARGET_DAY}).json()

    # Strain and activity must not appear as blocks (unshipped in v0.1.0).
    assert "strain" not in body
    assert "activity" not in body
    # The training recommendation *does* ship (a deterministic label).
    assert "training_recommendation" in body

    gap_codes = {g["code"] for g in body["data_gaps"]}
    assert "strain_not_available" in gap_codes
    assert "activity_not_available" in gap_codes
    assert "training_recommendation_not_available" not in gap_codes
    assert "missing_hrv_or_resting_hr" in gap_codes


def test_no_sleep_day_omits_the_block_and_discloses_gap(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    _login(client, factory)  # no sleep seeded
    body = client.get("/v1/today", params={"day": TARGET_DAY}).json()

    # No phantom zero-duration sleep measurement leaks through.
    assert body["sleep"] is None
    gap_codes = {g["code"] for g in body["data_gaps"]}
    assert "missing_authoritative_sleep" in gap_codes


def test_gaps_are_deduplicated(client: TestClient, factory: sessionmaker[Session]) -> None:
    # missing_authoritative_sleep is disclosed by both the composite and the
    # recovery gate when sleep is absent; it must appear exactly once.
    _login(client, factory)
    body = client.get("/v1/today", params={"day": TARGET_DAY}).json()
    codes = [g["code"] for g in body["data_gaps"]]
    assert codes.count("missing_authoritative_sleep") == 1


def test_malformed_day_is_rejected(client: TestClient, factory: sessionmaker[Session]) -> None:
    _login(client, factory)
    response = client.get("/v1/today", params={"day": "not-a-date"})
    assert response.status_code == 422
