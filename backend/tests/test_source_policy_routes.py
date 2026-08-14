"""``/v1/source-policies`` — the inspectable-policy requirement, over HTTP.

ADR 0005 requires the policy to be inspectable and the product principles
require a user to be able to audit *why* a day looks the way it does. These
drive both halves: the rule in force, and what it actually decided.
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
from akunaki.adapters.db.fact_repository import FactRepository
from akunaki.adapters.db.models import FactRecord, Tenant, User
from akunaki.adapters.db.session_repository import SessionRepository
from akunaki.adapters.db.source_selection_repository import SourceSelectionRepository
from akunaki.api.app import create_app
from akunaki.api.security import SESSION_COOKIE_NAME
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339
from akunaki.domain.sleep_normalizer import SleepFact
from akunaki.domain.source_policy import (
    SLEEP_METRIC_FAMILY,
    SOURCE_POLICY_VERSION,
    decide_sleep_selection,
)

T0 = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
DAY = "2026-08-04"


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def route_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "source_policy_routes.db"
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


def _login(client: TestClient, factory: sessionmaker[Session]) -> None:
    issued = SessionRepository(factory).issue(
        session_id="sess-user-1",
        user_id="user-1",
        now=datetime.now(UTC),
        ttl=timedelta(hours=12),
    )
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, issued.token)


def _seed_sleep(factory: sessionmaker[Session], *, provider: str, fact_id: str) -> None:
    """Write one sleep fact through the real fact-write path."""
    FactRepository(factory).write_sleep_fact(
        fact_record_id=fact_id,
        tenant_id="tenant-1",
        connection_id=None,
        fact=SleepFact(
            vendor_record_id=fact_id,
            start_utc=f"{DAY}T00:00:00Z",
            end_utc=f"{DAY}T07:00:00Z",
            local_health_day=DAY,
            source_offset_minutes=0,
            iana_timezone="UTC",
            is_nap=False,
            duration_min=420.0,
            time_in_bed_min=460.0,
            efficiency_pct=None,
            light_min=None,
            deep_min=None,
            rem_min=None,
            awake_min=None,
            quality="high",
            confidence=1.0,
            content_hash=fact_id,
        ),
        raw_revision_id=None,
        raw_payload_id=None,
        schema_version="v1",
        now=T0,
    )
    # The write path stamps provider from the normalizer; force the one we want.
    with factory() as session, session.begin():
        row = session.get(FactRecord, fact_id)
        assert row is not None
        row.provider = provider


def _record_decision(factory: sessionmaker[Session], *, by_provider: dict[str, list[str]]) -> None:
    decision = decide_sleep_selection(by_provider)
    ids = iter(f"cand-{n}" for n in range(1, 50))
    SourceSelectionRepository(factory).record_daily_selection(
        selection_id="sel-1",
        tenant_id="tenant-1",
        policy_version=SOURCE_POLICY_VERSION,
        spec=decision.to_spec(local_health_day=DAY),
        new_candidate_id=lambda: next(ids),
        now=T0,
    )


# ---------------------------------------------------------------------------
# The rule in force
# ---------------------------------------------------------------------------


def test_effective_policy_requires_a_session() -> None:
    client = TestClient(create_app(Settings(database_url="sqlite+libsql:///:memory:")))
    assert client.get("/v1/source-policies/effective").status_code == 401


def test_effective_policy_discloses_the_precedence(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """A user can see which provider wins, and in what order."""
    _login(client, factory)

    body = client.get("/v1/source-policies/effective").json()

    assert body["policy_version"] == SOURCE_POLICY_VERSION
    by_family = {f["metric_family"]: f["providers"] for f in body["families"]}
    # Ordered most-authoritative first: Oura wins any night it covers.
    assert by_family[SLEEP_METRIC_FAMILY] == ["oura", "google_health"]


def test_effective_policy_lists_only_enforced_families(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """Every listed family must be one the engine really selects between.

    Listing an unenforced family would present an aspiration as a rule, which
    is exactly what an inspectable policy must not do. Each family here has a
    precedence the decision path consults, and each precedence names only
    providers actually worn for that family — padding one would invent an
    authoritative answer from a device that was never measuring it.
    """
    _login(client, factory)

    by_family = {
        f["metric_family"]: f["providers"]
        for f in client.get("/v1/source-policies/effective").json()["families"]
    }

    assert set(by_family) == {"sleep_session", "nap", "workout", "daily_activity"}
    # Naps invert the sleep order: the ring is off during the day.
    assert by_family["nap"][0] == "google_health"
    assert by_family[SLEEP_METRIC_FAMILY][0] == "oura"
    # Only the sports watch is worn for training; no fallback invents a workout.
    assert by_family["workout"] == ["polar"]


# ---------------------------------------------------------------------------
# What it actually decided
# ---------------------------------------------------------------------------


def test_decision_explains_a_resolved_conflict(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """The product question: which source won my night, and what else was there."""
    _seed_sleep(factory, provider="oura", fact_id="o1")
    _seed_sleep(factory, provider="google_health", fact_id="g1")
    _record_decision(factory, by_provider={"oura": ["o1"], "google_health": ["g1"]})
    _login(client, factory)

    body = client.get("/v1/source-policies/decisions", params={"day": DAY}).json()

    assert body["selected_provider"] == "oura"
    assert body["selection_reason"] == "policy_match"
    assert body["policy_version"] == SOURCE_POLICY_VERSION
    # The loser stays visible — never averaged, never a silent fallback.
    assert [c["provider"] for c in body["candidates"]] == ["oura", "google_health"]
    assert body["candidates"][1]["reason"] == "lower_precedence"


def test_decision_discloses_providers_never_fact_ids(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """The user's question is which source won, not which row.

    The provenance surface already refuses to hand out ids; this one must not
    become the back door.
    """
    _seed_sleep(factory, provider="oura", fact_id="o1")
    _record_decision(factory, by_provider={"oura": ["o1"]})
    _login(client, factory)

    raw = client.get("/v1/source-policies/decisions", params={"day": DAY}).text

    assert "o1" not in raw
    assert "fact_record_id" not in raw


def test_single_provider_day_reads_as_only_source(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    _seed_sleep(factory, provider="google_health", fact_id="g1")
    _record_decision(factory, by_provider={"google_health": ["g1"]})
    _login(client, factory)

    body = client.get("/v1/source-policies/decisions", params={"day": DAY}).json()

    assert body["selected_provider"] == "google_health"
    assert body["selection_reason"] == "only_source"


def test_day_with_no_decision_is_404(client: TestClient, factory: sessionmaker[Session]) -> None:
    """Nothing recorded is not a default; inventing one would misreport."""
    _login(client, factory)

    assert client.get("/v1/source-policies/decisions", params={"day": DAY}).status_code == 404


def test_another_tenants_decision_is_not_readable(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """A decision is only visible to the tenant it was recorded for."""
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
    # A fact and a decision that belong entirely to the other tenant.
    FactRepository(factory).write_sleep_fact(
        fact_record_id="theirs",
        tenant_id="tenant-2",
        connection_id=None,
        fact=SleepFact(
            vendor_record_id="theirs",
            start_utc=f"{DAY}T00:00:00Z",
            end_utc=f"{DAY}T07:00:00Z",
            local_health_day=DAY,
            source_offset_minutes=0,
            iana_timezone="UTC",
            is_nap=False,
            duration_min=420.0,
            time_in_bed_min=460.0,
            efficiency_pct=None,
            light_min=None,
            deep_min=None,
            rem_min=None,
            awake_min=None,
            quality="high",
            confidence=1.0,
            content_hash="theirs",
        ),
        raw_revision_id=None,
        raw_payload_id=None,
        schema_version="v1",
        now=T0,
    )
    decision = decide_sleep_selection({"oura": ["theirs"]})
    ids = iter(f"tc-{n}" for n in range(1, 50))
    SourceSelectionRepository(factory).record_daily_selection(
        selection_id="sel-theirs",
        tenant_id="tenant-2",
        policy_version=SOURCE_POLICY_VERSION,
        spec=decision.to_spec(local_health_day=DAY),
        new_candidate_id=lambda: next(ids),
        now=T0,
    )
    _login(client, factory)

    # tenant-1 is the caller and has no decision of its own for this day.
    assert client.get("/v1/source-policies/decisions", params={"day": DAY}).status_code == 404


def test_malformed_day_is_422(client: TestClient, factory: sessionmaker[Session]) -> None:
    _login(client, factory)

    response = client.get("/v1/source-policies/decisions", params={"day": "04-08-2026"})

    assert response.status_code == 422


def test_an_unknown_family_is_named_not_folded_into_no_decision(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """A typo and "nothing recorded" mean opposite things.

    `no_decision` says the engine recorded nothing for a day it understands. An
    unrecognized family means the question was never askable — and a client that
    cannot tell them apart reads a misspelling as "this family has no data" and
    stops looking.
    """
    _login(client, factory)

    body = client.get(
        "/v1/source-policies/decisions",
        params={"day": "2026-08-04", "metric_family": "sleep_sesion"},
    )

    assert body.status_code == 404
    assert body.json()["detail"]["code"] == "unknown_metric_family"
    # The rejected name is echoed so the client can see its own typo.
    assert body.json()["detail"]["metric_family"] == "sleep_sesion"


def test_a_known_family_with_no_data_still_reads_no_decision(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """The other half of the distinction: a real family, genuinely empty."""
    _login(client, factory)

    body = client.get(
        "/v1/source-policies/decisions",
        params={"day": "2026-08-04", "metric_family": "workout"},
    )

    assert body.status_code == 404
    assert body.json()["detail"]["code"] == "no_decision"


def test_every_advertised_family_is_answerable(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """A family the policy surface advertises must be a valid question.

    Validating against a hand-kept list would drift from the precedence table
    and start rejecting a family the engine really does decide.
    """
    _login(client, factory)

    families = [
        f["metric_family"] for f in client.get("/v1/source-policies/effective").json()["families"]
    ]

    for family in families:
        body = client.get(
            "/v1/source-policies/decisions",
            params={"day": "2026-08-04", "metric_family": family},
        )
        assert body.json()["detail"]["code"] != "unknown_metric_family", family
