"""``GET /v1/metrics/{metric}`` over real HTTP.

A metric detail view shows measurements, never a score, and must not fill gaps
with zeros — a zero-filled chart shows a real measurement of nothing.
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
    DailyActivity,
    FactRecord,
    OvernightVitals,
    Tenant,
    User,
)
from akunaki.adapters.db.session_repository import SessionRepository
from akunaki.api.app import create_app
from akunaki.api.security import SESSION_COOKIE_NAME
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339

T0 = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
DAY = "2026-08-04"


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def route_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "metric_series.db"
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


def _seed_hrv(
    factory: sessionmaker[Session],
    *,
    day: str,
    hrv_ms: float,
    tenant_id: str = "tenant-1",
) -> None:
    fact_id = f"{tenant_id}-{day}"
    with factory() as session, session.begin():
        session.add(
            FactRecord(
                id=fact_id,
                tenant_id=tenant_id,
                connection_id=None,
                provider="oura",
                entity_type="overnight_vitals",
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
                normalizer_version="oura_vitals_v0.1.0",
                content_hash=fact_id,
                fact_key=f"overnight_vitals:{fact_id}",
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
            OvernightVitals(
                fact_record_id=fact_id,
                tenant_id=tenant_id,
                hrv_ms=hrv_ms,
                resting_hr_bpm=None,
                temperature_deviation_c=None,
                respiratory_rate_bpm=None,
            )
        )


def _days_before(day: str, count: int) -> list[str]:
    end = datetime.fromisoformat(day).date()
    return [(end - timedelta(days=n)).isoformat() for n in range(count)]


def test_requires_a_session() -> None:
    client = TestClient(create_app(Settings(database_url="sqlite+libsql:///:memory:")))
    assert client.get("/v1/metrics/hrv", params={"day": DAY}).status_code == 401


def test_lists_the_supported_metrics(client: TestClient, factory: sessionmaker[Session]) -> None:
    """A client should not discover metric names by 404."""
    _login(client, factory)

    metrics = client.get("/v1/metrics").json()["metrics"]

    assert "hrv" in metrics
    assert "sleep_duration" in metrics
    assert metrics == sorted(metrics)


def test_unknown_metric_is_404(client: TestClient, factory: sessionmaker[Session]) -> None:
    """An unexposed name is a client error, not an empty series reading as no data."""
    _login(client, factory)

    response = client.get("/v1/metrics/blood_pressure", params={"day": DAY})

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "unknown_metric"


def test_series_returns_measured_days_oldest_first(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    for offset, day in enumerate(_days_before(DAY, 3)):
        _seed_hrv(factory, day=day, hrv_ms=60.0 + offset)
    _login(client, factory)

    body = client.get("/v1/metrics/hrv", params={"day": DAY, "window_days": 3}).json()

    assert body["metric"] == "hrv"
    assert body["unit"] == "ms"
    days = [p["local_health_day"] for p in body["points"]]
    assert days == sorted(days)
    assert len(days) == 3


def test_gaps_are_omitted_not_zero_filled(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """A zero-filled chart would show a real measurement of nothing."""
    _seed_hrv(factory, day=DAY, hrv_ms=62.0)
    _login(client, factory)

    body = client.get("/v1/metrics/hrv", params={"day": DAY, "window_days": 7}).json()

    assert body["window_days"] == 7
    assert body["known_days"] == 1
    assert body["coverage_is_partial"] is True
    assert [p["value"] for p in body["points"]] == [62.0]
    # No zero-valued days were invented for the six unknown ones.
    assert all(p["value"] != 0.0 for p in body["points"])


def test_sparse_window_reports_an_insufficient_baseline(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """Too few samples must not produce a confident-looking band."""
    for day in _days_before(DAY, 3):
        _seed_hrv(factory, day=day, hrv_ms=60.0)
    _login(client, factory)

    body = client.get("/v1/metrics/hrv", params={"day": DAY, "window_days": 30}).json()

    assert body["baseline_maturity"] == "insufficient"
    assert body["baseline_center"] is None
    assert body["baseline_robust_scale"] is None


def test_dense_window_reports_a_usable_baseline(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """With enough samples a chart can draw a band around the centre."""
    for offset, day in enumerate(_days_before(DAY, 30)):
        _seed_hrv(factory, day=day, hrv_ms=58.0 if offset % 2 else 62.0)
    _login(client, factory)

    body = client.get("/v1/metrics/hrv", params={"day": DAY, "window_days": 30}).json()

    assert body["known_days"] == 30
    assert body["coverage_is_partial"] is False
    assert body["baseline_maturity"] == "mature"
    assert body["baseline_center"] == pytest.approx(60.0)
    assert body["baseline_robust_scale"] is not None


def test_series_carries_no_score(client: TestClient, factory: sessionmaker[Session]) -> None:
    """v0.1.0 ships one score code; a per-metric rating would imply another."""
    _seed_hrv(factory, day=DAY, hrv_ms=62.0)
    _login(client, factory)

    body = client.get("/v1/metrics/hrv", params={"day": DAY}).json()

    assert "score" not in body
    assert not any("score" in key for key in body)


def test_never_serves_another_tenants_measurements(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    _seed_hrv(factory, day=DAY, hrv_ms=99.0, tenant_id="tenant-2")
    _login(client, factory)

    body = client.get("/v1/metrics/hrv", params={"day": DAY}).json()

    assert body["points"] == []
    assert body["known_days"] == 0


def test_malformed_day_is_422(client: TestClient, factory: sessionmaker[Session]) -> None:
    _login(client, factory)

    assert client.get("/v1/metrics/hrv", params={"day": "04-08-2026"}).status_code == 422


def test_window_is_bounded(client: TestClient, factory: sessionmaker[Session]) -> None:
    """A caller cannot ask for an unbounded scan."""
    _login(client, factory)

    response = client.get("/v1/metrics/hrv", params={"day": DAY, "window_days": 5000})

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Multi-metric trends
# ---------------------------------------------------------------------------


def _seed_steps(factory: sessionmaker[Session], *, day: str, steps: float) -> None:
    fact_id = f"steps-{day}"
    with factory() as session, session.begin():
        session.add(
            FactRecord(
                id=fact_id,
                tenant_id="tenant-1",
                connection_id=None,
                provider="google_health",
                entity_type="daily_activity",
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
                normalizer_version="google_activity_v0.1.0",
                content_hash=fact_id,
                fact_key=f"daily_activity:{fact_id}",
                version_n=1,
                is_current=1,
                superseded_by=None,
                superseded_at=None,
                deletion_state="active",
                exclude_from_load=0,
                created_at=NOW_S,
            )
        )
        session.flush()
        session.add(
            DailyActivity(
                fact_record_id=fact_id,
                tenant_id="tenant-1",
                steps=int(steps),
                active_minutes=None,
            )
        )


def test_trends_requires_a_session() -> None:
    client = TestClient(create_app(Settings(database_url="sqlite+libsql:///:memory:")))
    response = client.get("/v1/trends", params={"day": DAY, "metric": "hrv"})
    assert response.status_code == 401


def test_trends_returns_several_metrics_in_request_order(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """One request instead of a client fanning out N calls."""
    _seed_hrv(factory, day=DAY, hrv_ms=62.0)
    _seed_steps(factory, day=DAY, steps=9000.0)
    _login(client, factory)

    body = client.get(
        "/v1/trends", params=[("day", DAY), ("metric", "steps"), ("metric", "hrv")]
    ).json()

    # Order preserved so a client can pair response with request by position.
    assert [s["metric"] for s in body["series"]] == ["steps", "hrv"]
    assert body["window_days"] == 30


def test_trends_agree_with_the_single_metric_read(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """Same service, so a trend and a detail view cannot disagree.

    A second query path would eventually drift; this pins that they share one.
    """
    for offset, day in enumerate(_days_before(DAY, 20)):
        _seed_hrv(factory, day=day, hrv_ms=58.0 if offset % 2 else 62.0)
    _login(client, factory)

    single = client.get("/v1/metrics/hrv", params={"day": DAY, "window_days": 20}).json()
    [trend] = client.get(
        "/v1/trends", params={"day": DAY, "metric": "hrv", "window_days": 20}
    ).json()["series"]

    assert trend == single


def test_trends_reject_an_unknown_metric(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """Named explicitly rather than dropped from the list.

    A shorter list would read as "no data for that metric" instead of "that
    metric does not exist".
    """
    _login(client, factory)

    response = client.get(
        "/v1/trends", params=[("day", DAY), ("metric", "hrv"), ("metric", "nope")]
    )

    assert response.status_code == 404
    assert response.json()["detail"]["metric"] == "nope"


def test_trends_require_at_least_one_metric(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    _login(client, factory)

    assert client.get("/v1/trends", params={"day": DAY}).status_code == 422


def test_trends_bound_the_metric_count(client: TestClient, factory: sessionmaker[Session]) -> None:
    """The payload is metrics x window, so the metric count is the real bound."""
    _login(client, factory)
    params = [("day", DAY), *[("metric", "hrv") for _ in range(20)]]

    response = client.get("/v1/trends", params=params)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "too_many_metrics"


def test_trends_never_serve_another_tenant(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    _seed_hrv(factory, day=DAY, hrv_ms=99.0, tenant_id="tenant-2")
    _login(client, factory)

    [series] = client.get("/v1/trends", params={"day": DAY, "metric": "hrv"}).json()["series"]

    assert series["points"] == []
