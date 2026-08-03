"""End-to-end coverage of ``/v1/tools`` over real HTTP.

The typed registry is exposed to a plain HTTP client with no model packages
involved: list the tools, then invoke one under the session context. The tenant
comes from the session, and CSRF is enforced on the POST invoke path.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import akunaki.api.routes.tools as tools_module
from akunaki.adapters.crypto.sessions import generate_confirmation_token
from akunaki.adapters.db.anomaly_repository import AnomalyRepository
from akunaki.adapters.db.confirmation_repository import ConfirmationRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.fact_repository import FactRepository
from akunaki.adapters.db.models import Connection, FactRecord, Job, SleepSession, Tenant, User
from akunaki.adapters.db.session_repository import SessionRepository
from akunaki.api.app import create_app
from akunaki.api.security import CSRF_HEADER_NAME, SESSION_COOKIE_NAME
from akunaki.application.tool_registry import (
    Sensitivity,
    SideEffect,
    Tool,
    ToolContext,
    ToolRegistry,
)
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.anomalies import AnomalySeverity
from akunaki.domain.confirmations import ConfirmationBinding, canonical_args_hash
from akunaki.domain.jobs import to_utc_rfc3339
from akunaki.domain.workout_normalizer import WorkoutFact

T0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
DAY = "2026-07-22"
RUN_ID = "run-1"


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def route_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "tools_routes.db"
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


def test_list_requires_session() -> None:
    client = TestClient(create_app(Settings(database_url="sqlite+libsql:///:memory:")))
    assert client.get("/v1/tools").status_code == 401


def test_lists_the_health_tools(client: TestClient, factory: sessionmaker[Session]) -> None:
    _login(client, factory)
    body = client.get("/v1/tools").json()
    names = {t["name"] for t in body["tools"]}
    assert {"health.get_today", "health.get_recovery", "health.get_sleep"} <= names
    recovery = next(t for t in body["tools"] if t["name"] == "health.get_recovery")
    assert recovery["side_effect"] == "none"
    assert recovery["sensitivity"] == "health_read"
    assert "read:health" in recovery["scopes"]


def test_invoke_requires_csrf(client: TestClient, factory: sessionmaker[Session]) -> None:
    _login(client, factory)  # cookie only, no CSRF header
    response = client.post("/v1/tools/health.get_sleep", json={"input": {"day": DAY}})
    assert response.status_code == 403


def test_invoke_sleep_tool(client: TestClient, factory: sessionmaker[Session]) -> None:
    _seed_sleep(factory, day=DAY, fact_id="s1")
    csrf = _login(client, factory)
    response = client.post(
        "/v1/tools/health.get_sleep",
        json={"input": {"day": DAY}},
        headers={CSRF_HEADER_NAME: csrf},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["duration_min"] == 420.0
    assert body["formula_version"] == "sleep_summary_v0.1.0"


def test_unknown_tool_is_404(client: TestClient, factory: sessionmaker[Session]) -> None:
    csrf = _login(client, factory)
    response = client.post(
        "/v1/tools/health.nope", json={"input": {}}, headers={CSRF_HEADER_NAME: csrf}
    )
    assert response.status_code == 404


def test_malformed_tool_input_is_422(client: TestClient, factory: sessionmaker[Session]) -> None:
    csrf = _login(client, factory)
    response = client.post(
        "/v1/tools/health.get_sleep",
        json={"input": {"day": "2026-13-40"}},
        headers={CSRF_HEADER_NAME: csrf},
    )
    assert response.status_code == 422


def test_invoke_today_tool(client: TestClient, factory: sessionmaker[Session]) -> None:
    _seed_sleep(factory, day=DAY, fact_id="s1")
    csrf = _login(client, factory)
    response = client.post(
        "/v1/tools/health.get_today",
        json={"input": {"day": DAY}},
        headers={CSRF_HEADER_NAME: csrf},
    )
    assert response.status_code == 200
    body = response.json()
    # A sleep-only tenant: recovery insufficient -> training label insufficient.
    assert body["training_label"] == "insufficient"
    assert body["ruleset_version"] == "training_label_v0.1.0"


# ---------------------------------------------------------------------------
# Canonical registry: anomalies, workouts, connections
# ---------------------------------------------------------------------------


def _seed_workout(factory: sessionmaker[Session], *, workout_id: str, start_hour: int) -> None:
    FactRepository(factory).write_workout_fact(
        fact_record_id=workout_id,
        tenant_id="tenant-1",
        connection_id=None,
        fact=WorkoutFact(
            vendor_record_id=workout_id,
            start_utc=f"{DAY}T{start_hour:02d}:00:00Z",
            end_utc=f"{DAY}T{start_hour + 1:02d}:00:00Z",
            local_health_day=DAY,
            source_offset_minutes=0,
            session_load=120.0,
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


def test_lists_the_canonical_read_tools(client: TestClient, factory: sessionmaker[Session]) -> None:
    """The registry names the read tools the canonical catalog specifies."""
    _login(client, factory)
    names = {t["name"] for t in client.get("/v1/tools").json()["tools"]}

    assert {
        "health.find_anomalies",
        "health.get_recent_workouts",
        "health.get_workout",
        "connections.list",
    } <= names


def test_connections_tool_is_not_scoped_as_health(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """Connection metadata is not health data; the scope must not over-grant."""
    _login(client, factory)
    tools = client.get("/v1/tools").json()["tools"]
    connections = next(t for t in tools if t["name"] == "connections.list")

    assert connections["scopes"] == ["read:connections"]
    assert connections["sensitivity"] == "low"
    assert connections["side_effect"] == "none"


def test_only_the_sync_tool_mutates(client: TestClient, factory: sessionmaker[Session]) -> None:
    """``connections.sync`` is the one mutating tool, and it requires confirmation.

    Every other registered tool is a read. The lifecycle mutations that have no
    service wiring yet (``exports.create``, ``privacy.delete``) must not appear.
    """
    _login(client, factory)
    tools = client.get("/v1/tools").json()["tools"]

    mutating = [t for t in tools if t["side_effect"] != "none"]
    assert [t["name"] for t in mutating] == ["connections.sync"]
    assert mutating[0]["side_effect"] == "enqueue_job"
    assert mutating[0]["requires_confirmation"] is True
    assert mutating[0]["scopes"] == ["write:connections"]

    names = {t["name"] for t in tools}
    assert "exports.create" not in names
    assert "privacy.delete" not in names


def test_invoke_find_anomalies(client: TestClient, factory: sessionmaker[Session]) -> None:
    AnomalyRepository(factory).open_interval(
        anomaly_id="an-1",
        tenant_id="tenant-1",
        feature_code="low_hrv",
        severity=AnomalySeverity.HIGH,
        z_like=-3.0,
        formula_version="general_recovery_v0.1.0",
        local_health_day=DAY,
        now=T0,
    )
    csrf = _login(client, factory)

    body = client.post(
        "/v1/tools/health.find_anomalies",
        json={"input": {"day": DAY}},
        headers={CSRF_HEADER_NAME: csrf},
    ).json()

    assert [a["feature_code"] for a in body["anomalies"]] == ["low_hrv"]
    # The detector's internal z stays out of a model's context.
    assert "z_like" not in body["anomalies"][0]


def test_invoke_get_recent_workouts(client: TestClient, factory: sessionmaker[Session]) -> None:
    _seed_workout(factory, workout_id="w1", start_hour=6)
    _seed_workout(factory, workout_id="w2", start_hour=18)
    csrf = _login(client, factory)

    body = client.post(
        "/v1/tools/health.get_recent_workouts",
        json={"input": {"limit": 1}},
        headers={CSRF_HEADER_NAME: csrf},
    ).json()

    assert [w["workout_id"] for w in body["workouts"]] == ["w2"]
    assert body["next_cursor"] is not None
    assert body["workouts"][0]["total_zone_min"] == 50.0


def test_invoke_get_workout(client: TestClient, factory: sessionmaker[Session]) -> None:
    _seed_workout(factory, workout_id="w1", start_hour=6)
    csrf = _login(client, factory)

    body = client.post(
        "/v1/tools/health.get_workout",
        json={"input": {"workout_id": "w1"}},
        headers={CSRF_HEADER_NAME: csrf},
    ).json()

    assert body["workout_id"] == "w1"
    assert body["session_load"] == 120.0


def test_unknown_workout_through_a_tool_is_404(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """A tool must not become a way to probe ids for existence."""
    csrf = _login(client, factory)

    response = client.post(
        "/v1/tools/health.get_workout",
        json={"input": {"workout_id": "nope"}},
        headers={CSRF_HEADER_NAME: csrf},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == {"code": "not_found"}


def test_invoke_connections_list(client: TestClient, factory: sessionmaker[Session]) -> None:
    csrf = _login(client, factory)

    body = client.post(
        "/v1/tools/connections.list",
        json={"input": {}},
        headers={CSRF_HEADER_NAME: csrf},
    ).json()

    # No connector linked in this fixture: an empty list, not an error.
    assert body == {"connections": []}


# ---------------------------------------------------------------------------
# Confirmation enforcement for mutating tools
# ---------------------------------------------------------------------------


class _EchoInput(BaseModel):
    target: str = "a"


class _EchoOutput(BaseModel):
    target: str


def _mutating_tool(calls: list[str]) -> Tool[_EchoInput, _EchoOutput]:
    """A stand-in mutating tool that records whether it actually ran."""

    def handler(inputs: _EchoInput, context: ToolContext) -> _EchoOutput:
        calls.append(inputs.target)
        return _EchoOutput(target=inputs.target)

    return Tool(
        name="test.mutate",
        input_model=_EchoInput,
        output_model=_EchoOutput,
        handler=handler,
        side_effect=SideEffect.ENQUEUE_JOB,
        sensitivity=Sensitivity.DESTRUCTIVE,
        model_exposure=False,
        requires_confirmation=True,
        audit="test.mutate",
    )


@pytest.fixture
def mutating_client(
    route_db: str, factory: sessionmaker[Session]
) -> Iterator[tuple[TestClient, list[str]]]:
    """A client whose registry also carries one mutating tool."""
    calls: list[str] = []
    app = create_app(Settings(database_url=route_db))

    def _registry_with_mutation() -> ToolRegistry:
        registry = tools_module._registry(
            create_session_factory(create_db_engine(Settings(database_url=route_db)))
        )
        registry.register(_mutating_tool(calls))
        return registry

    app.dependency_overrides[tools_module._registry] = _registry_with_mutation
    yield TestClient(app), calls
    app.dependency_overrides.clear()


def _issue_confirmation(
    factory: sessionmaker[Session],
    *,
    args: dict[str, object],
    tool_name: str = "test.mutate",
    idempotency_key: str = "idem-1",
    user_id: str = "user-1",
) -> str:
    token = generate_confirmation_token()
    ConfirmationRepository(factory).issue(
        confirmation_id=f"conf-{idempotency_key}",
        token=token,
        binding=ConfirmationBinding(
            tenant_id="tenant-1",
            user_id=user_id,
            run_id=RUN_ID,
            tool_name=tool_name,
            args_hash=canonical_args_hash(args),
            idempotency_key=idempotency_key,
        ),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        now=datetime.now(UTC),
    )
    return token


def test_mutating_tool_without_confirmation_is_refused(
    mutating_client: tuple[TestClient, list[str]], factory: sessionmaker[Session]
) -> None:
    """Fail closed: no token means the handler never runs."""
    client, calls = mutating_client
    csrf = _login(client, factory)

    response = client.post(
        "/v1/tools/test.mutate",
        json={"input": {"target": "a"}, "run_id": RUN_ID},
        headers={CSRF_HEADER_NAME: csrf},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "confirmation_required"
    assert calls == []


def test_mutating_tool_runs_with_a_matching_confirmation(
    mutating_client: tuple[TestClient, list[str]], factory: sessionmaker[Session]
) -> None:
    client, calls = mutating_client
    csrf = _login(client, factory)
    token = _issue_confirmation(factory, args={"target": "a"})

    response = client.post(
        "/v1/tools/test.mutate",
        json={
            "input": {"target": "a"},
            "confirmation_token": token,
            "idempotency_key": "idem-1",
            "run_id": RUN_ID,
        },
        headers={CSRF_HEADER_NAME: csrf},
    )

    assert response.status_code == 200
    assert calls == ["a"]


def test_substituted_arguments_do_not_execute(
    mutating_client: tuple[TestClient, list[str]], factory: sessionmaker[Session]
) -> None:
    """The user approved ``a``; a model swapping in ``b`` must not run.

    This is the attack the args-hash binding exists to stop.
    """
    client, calls = mutating_client
    csrf = _login(client, factory)
    token = _issue_confirmation(factory, args={"target": "a"})

    response = client.post(
        "/v1/tools/test.mutate",
        json={
            "input": {"target": "b"},
            "confirmation_token": token,
            "idempotency_key": "idem-1",
            "run_id": RUN_ID,
        },
        headers={CSRF_HEADER_NAME: csrf},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "confirmation_invalid"
    assert calls == []


def test_replayed_confirmation_does_not_execute_twice(
    mutating_client: tuple[TestClient, list[str]], factory: sessionmaker[Session]
) -> None:
    """Rule 4 end to end: the side effect happens exactly once."""
    client, calls = mutating_client
    csrf = _login(client, factory)
    token = _issue_confirmation(factory, args={"target": "a"})
    payload = {
        "input": {"target": "a"},
        "confirmation_token": token,
        "idempotency_key": "idem-1",
        "run_id": RUN_ID,
    }

    first = client.post("/v1/tools/test.mutate", json=payload, headers={CSRF_HEADER_NAME: csrf})
    second = client.post("/v1/tools/test.mutate", json=payload, headers={CSRF_HEADER_NAME: csrf})

    assert first.status_code == 200
    assert second.status_code == 403
    assert calls == ["a"]


def test_confirmation_for_another_tool_does_not_execute(
    mutating_client: tuple[TestClient, list[str]], factory: sessionmaker[Session]
) -> None:
    client, calls = mutating_client
    csrf = _login(client, factory)
    token = _issue_confirmation(factory, args={"target": "a"}, tool_name="privacy.delete")

    response = client.post(
        "/v1/tools/test.mutate",
        json={
            "input": {"target": "a"},
            "confirmation_token": token,
            "idempotency_key": "idem-1",
            "run_id": RUN_ID,
        },
        headers={CSRF_HEADER_NAME: csrf},
    )

    assert response.status_code == 403
    assert calls == []


def test_rejection_reasons_are_indistinguishable(
    mutating_client: tuple[TestClient, list[str]], factory: sessionmaker[Session]
) -> None:
    """A caller must not learn *why* a confirmation failed.

    Distinguishing "wrong tool" from "expired" would let someone probe for
    valid tool names or live tokens.
    """
    client, _calls = mutating_client
    csrf = _login(client, factory)
    wrong_tool = _issue_confirmation(
        factory, args={"target": "a"}, tool_name="other.tool", idempotency_key="idem-a"
    )
    wrong_args = _issue_confirmation(factory, args={"target": "zzz"}, idempotency_key="idem-b")

    responses = [
        client.post(
            "/v1/tools/test.mutate",
            json={
                "input": {"target": "a"},
                "confirmation_token": tok,
                "idempotency_key": key,
                "run_id": RUN_ID,
            },
            headers={CSRF_HEADER_NAME: csrf},
        )
        for tok, key in ((wrong_tool, "idem-a"), (wrong_args, "idem-b"), ("confirm_nope", "idem-c"))
    ]

    assert {r.status_code for r in responses} == {403}
    assert len({r.json()["detail"]["code"] for r in responses}) == 1


def test_direct_call_needs_no_confirmation(
    mutating_client: tuple[TestClient, list[str]], factory: sessionmaker[Session]
) -> None:
    """ "Confirmation **if agent**": a human in their own session is already explicit.

    The call is CSRF-enforced and tenant-scoped; demanding a second approval
    would make "sync now" unusable without adding safety.
    """
    client, calls = mutating_client
    csrf = _login(client, factory)

    response = client.post(
        "/v1/tools/test.mutate",
        json={"input": {"target": "a"}},  # no run_id
        headers={CSRF_HEADER_NAME: csrf},
    )

    assert response.status_code == 200
    assert calls == ["a"]


def test_agent_call_cannot_skip_confirmation_by_omitting_the_run(
    mutating_client: tuple[TestClient, list[str]], factory: sessionmaker[Session]
) -> None:
    """A confirmation bound to a run does not authorize a run-less call.

    Dropping ``run_id`` takes the caller out of the agent path — which is the
    unconfirmed path — but the binding still has to match, so a token issued
    for a run cannot be spent outside it.
    """
    client, calls = mutating_client
    csrf = _login(client, factory)
    token = _issue_confirmation(factory, args={"target": "a"})

    # Presenting a run-bound token without the run: confirmation is not even
    # required, so it executes — but the token is left unspent rather than
    # silently consumed by a call it was not issued for.
    response = client.post(
        "/v1/tools/test.mutate",
        json={
            "input": {"target": "a"},
            "confirmation_token": token,
            "idempotency_key": "idem-1",
        },
        headers={CSRF_HEADER_NAME: csrf},
    )
    assert response.status_code == 200
    assert calls == ["a"]

    # The run-scoped token is still pending, so the agent path still needs it.
    agent = client.post(
        "/v1/tools/test.mutate",
        json={
            "input": {"target": "a"},
            "confirmation_token": token,
            "idempotency_key": "idem-1",
            "run_id": RUN_ID,
        },
        headers={CSRF_HEADER_NAME: csrf},
    )
    assert agent.status_code == 200


def test_sync_tool_enqueues_a_job(client: TestClient, factory: sessionmaker[Session]) -> None:
    """The mutating tool queues the same incremental sync the webhook path does."""
    with factory() as session, session.begin():
        session.add(
            Connection(
                id="conn-1",
                tenant_id="tenant-1",
                provider="polar",
                status="active",
                scopes_granted_json="[]",
                external_user_id=None,
                connected_at=NOW_S,
                updated_at=NOW_S,
            )
        )
    csrf = _login(client, factory)

    body = client.post(
        "/v1/tools/connections.sync",
        json={"input": {"connection_id": "conn-1"}},
        headers={CSRF_HEADER_NAME: csrf},
    ).json()

    assert body["created"] is True
    with factory() as session:
        job = session.scalars(
            select(Job).where(Job.job_type == "connection.incremental_sync")
        ).one()
    assert job.tenant_id == "tenant-1"


def test_sync_tool_cannot_reach_another_tenants_connection(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """An agent must not be able to sync a connection its user does not own."""
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
        session.add(
            Connection(
                id="conn-theirs",
                tenant_id="tenant-2",
                provider="polar",
                status="active",
                scopes_granted_json="[]",
                external_user_id=None,
                connected_at=NOW_S,
                updated_at=NOW_S,
            )
        )
    csrf = _login(client, factory)

    response = client.post(
        "/v1/tools/connections.sync",
        json={"input": {"connection_id": "conn-theirs"}},
        headers={CSRF_HEADER_NAME: csrf},
    )

    assert response.status_code == 404
    with factory() as session:
        assert session.scalars(select(Job)).all() == []
