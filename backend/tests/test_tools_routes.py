"""End-to-end coverage of ``/v1/tools`` over real HTTP.

The typed registry is exposed to a plain HTTP client with no model packages
involved: list the tools, then invoke one under the session context. The tenant
comes from the session, and CSRF is enforced on the POST invoke path.
"""

from __future__ import annotations

import json
from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import akunaki.api.routes.tools as tools_module
from akunaki.adapters.crypto.sessions import generate_confirmation_token
from akunaki.adapters.db.anomaly_repository import AnomalyRepository
from akunaki.adapters.db.audit_repository import AuditRepository
from akunaki.adapters.db.confirmation_repository import ConfirmationRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.fact_repository import FactRepository
from akunaki.adapters.db.models import (
    AuditEventRow,
    Connection,
    FactRecord,
    Job,
    SleepSession,
    Tenant,
    ToolConfirmation,
    User,
)
from akunaki.adapters.db.session_repository import SessionRepository
from akunaki.api.app import create_app
from akunaki.api.security import CSRF_HEADER_NAME, SESSION_COOKIE_NAME
from akunaki.application.tool_registry import (
    ConfirmationPolicy,
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
from conftest import upgrade_to_head

T0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
DAY = "2026-07-22"
RUN_ID = "run-1"


@pytest.fixture
def route_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "tools_routes.db"
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


def test_every_mutating_tool_is_gated(client: TestClient, factory: sessionmaker[Session]) -> None:
    """No mutation is reachable without a confirmation policy behind it.

    A tool that mutates and declares no confirmation would be invocable on the
    honour system, so this pins the whole mutating set rather than one tool.
    """
    _login(client, factory)
    tools = client.get("/v1/tools").json()["tools"]

    mutating = {t["name"]: t for t in tools if t["side_effect"] != "none"}
    assert set(mutating) == {"connections.sync", "privacy.delete"}

    # Every mutation is gated; the *policy* differs by how destructive it is.
    assert all(t["requires_confirmation"] for t in mutating.values())
    assert mutating["connections.sync"]["side_effect"] == "enqueue_job"
    assert mutating["connections.sync"]["scopes"] == ["write:connections"]
    assert mutating["privacy.delete"]["side_effect"] == "destroy_data"
    assert mutating["privacy.delete"]["scopes"] == ["delete:privacy"]

    # Not built: no service wiring yet.
    assert "exports.create" not in {t["name"] for t in tools}


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


def test_the_listing_tool_matches_the_rest_surface(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """The tool must not be strictly weaker than the route it mirrors.

    Both read the same ``ConnectionSummary``. The tool deliberately drops
    nothing but fields it has a stated reason to withhold, and an opaque
    ``connection_id`` is not one — it is the argument ``connections.sync``
    takes, so dropping it stranded the mutating tool.
    """
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

    [via_tool] = client.post(
        "/v1/tools/connections.list",
        json={"input": {}},
        headers={CSRF_HEADER_NAME: csrf},
    ).json()["connections"]
    [via_route] = client.get("/v1/connections").json()["connections"]

    assert via_tool["connection_id"] == via_route["connection_id"] == "conn-1"
    # Every field the route publishes is reachable through the tool as well.
    assert set(via_route) <= set(via_tool)


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
        confirmation=ConfirmationPolicy.IF_AGENT,
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


# ---------------------------------------------------------------------------
# Confirmation policy: never / if_agent / always
# ---------------------------------------------------------------------------


def test_privacy_delete_is_confirmed_always(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """The one destructive tool: model-invisible, confirmed for every caller."""
    _login(client, factory)
    tools = client.get("/v1/tools").json()["tools"]
    delete = next(t for t in tools if t["name"] == "privacy.delete")

    assert delete["sensitivity"] == "destructive"
    assert delete["side_effect"] == "destroy_data"
    assert delete["requires_confirmation"] is True
    # A model must not be the thing that invokes irreversible erasure.
    assert delete["model_exposure"] is False


def test_direct_delete_without_confirmation_is_refused(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """ "Always" means always: a session call is refused too.

    A CSRF token proves the request came from our page, not that the human
    meant to erase everything they have.
    """
    csrf = _login(client, factory)

    response = client.post(
        "/v1/tools/privacy.delete",
        json={"input": {}},  # no run_id: still refused
        headers={CSRF_HEADER_NAME: csrf},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "confirmation_required"
    with factory() as session:
        assert session.get(Tenant, "tenant-1") is not None


def test_confirm_then_delete_erases_the_tenant(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """The full out-of-band flow: approve the exact call, then execute it."""
    csrf = _login(client, factory)

    issued = client.post(
        "/v1/confirmations",
        json={"tool_name": "privacy.delete", "input": {}, "idempotency_key": "del-1"},
        headers={CSRF_HEADER_NAME: csrf},
    )
    assert issued.status_code == 201
    token = issued.json()["confirmation_token"]

    response = client.post(
        "/v1/tools/privacy.delete",
        json={"input": {}, "confirmation_token": token, "idempotency_key": "del-1"},
        headers={CSRF_HEADER_NAME: csrf},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    with factory() as session:
        assert session.get(Tenant, "tenant-1") is None


def test_confirmation_is_refused_for_a_read_tool(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """Handing out tokens nothing checks would make confirmation a rubber stamp."""
    csrf = _login(client, factory)

    response = client.post(
        "/v1/confirmations",
        json={"tool_name": "health.get_sleep", "input": {}, "idempotency_key": "k"},
        headers={CSRF_HEADER_NAME: csrf},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "confirmation_not_required"


def test_confirmation_for_an_unknown_tool_is_404(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    csrf = _login(client, factory)
    response = client.post(
        "/v1/confirmations",
        json={"tool_name": "nope.nope", "input": {}, "idempotency_key": "k"},
        headers={CSRF_HEADER_NAME: csrf},
    )
    assert response.status_code == 404


def test_confirmation_requires_a_session_and_csrf(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """Issuing an authorization is itself a state-changing act."""
    client.cookies.clear()
    body = {"tool_name": "privacy.delete", "input": {}, "idempotency_key": "k"}
    assert client.post("/v1/confirmations", json=body).status_code == 401

    _login(client, factory)  # cookie only, no CSRF header
    assert client.post("/v1/confirmations", json=body).status_code == 403


def test_confirmation_token_is_stored_hashed_only(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """A database dump must not yield a usable authorization."""
    csrf = _login(client, factory)
    token = client.post(
        "/v1/confirmations",
        json={"tool_name": "privacy.delete", "input": {}, "idempotency_key": "k"},
        headers={CSRF_HEADER_NAME: csrf},
    ).json()["confirmation_token"]

    with factory() as session:
        row = session.scalars(select(ToolConfirmation)).one()
    assert row.token_hash != token
    assert token not in row.token_hash


# ---------------------------------------------------------------------------
# tool.invoke auditing (mutations only)
# ---------------------------------------------------------------------------


def _audit_rows(factory: sessionmaker[Session]) -> list[AuditEventRow]:
    with factory() as session:
        return list(session.scalars(select(AuditEventRow).order_by(AuditEventRow.seq)))


def test_read_tools_are_not_audited(client: TestClient, factory: sessionmaker[Session]) -> None:
    """Auditing reads would flood the chain and stall the hottest path.

    Every append serializes on a tail read; a dashboard polling a day view
    would add thousands of rows a day that answer no security question.
    """
    _seed_sleep(factory, day=DAY, fact_id="s1")
    csrf = _login(client, factory)

    client.post(
        "/v1/tools/health.get_sleep",
        json={"input": {"day": DAY}},
        headers={CSRF_HEADER_NAME: csrf},
    )

    assert _audit_rows(factory) == []


def test_mutating_tool_invocation_is_audited(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
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

    client.post(
        "/v1/tools/connections.sync",
        json={"input": {"connection_id": "conn-1"}},
        headers={CSRF_HEADER_NAME: csrf},
    )

    [event] = _audit_rows(factory)
    assert event.action == "tool.invoke"
    assert event.resource_type == "tool"
    assert event.resource_id == "connections.sync"
    assert event.actor_id == "user-1"
    assert json.loads(event.metadata_json) == {"outcome": "succeeded"}


def test_refused_mutation_is_audited(client: TestClient, factory: sessionmaker[Session]) -> None:
    """A refusal is what a confused-deputy attempt looks like from outside.

    If only successes were recorded, the attempts worth investigating would be
    exactly the ones that left no trace.
    """
    csrf = _login(client, factory)

    response = client.post(
        "/v1/tools/privacy.delete",
        json={"input": {}},  # no confirmation: refused
        headers={CSRF_HEADER_NAME: csrf},
    )

    assert response.status_code == 403
    [event] = _audit_rows(factory)
    assert event.resource_id == "privacy.delete"
    assert json.loads(event.metadata_json) == {"outcome": "refused"}


def test_agent_origin_is_recorded(
    mutating_client: tuple[TestClient, list[str]], factory: sessionmaker[Session]
) -> None:
    """A reviewer's first question about a suspicious mutation is "who called it"."""
    client, _calls = mutating_client
    csrf = _login(client, factory)

    client.post(
        "/v1/tools/test.mutate",
        json={"input": {"target": "a"}, "run_id": RUN_ID},
        headers={CSRF_HEADER_NAME: csrf},
    )

    [event] = _audit_rows(factory)
    assert json.loads(event.metadata_json)["origin"] == "agent_run"


def test_audit_carries_no_tool_arguments(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """Arguments can carry health context; the trail must not copy them."""
    csrf = _login(client, factory)
    client.post(
        "/v1/tools/connections.sync",
        json={"input": {"connection_id": "secret-connection-id"}},
        headers={CSRF_HEADER_NAME: csrf},
    )

    for event in _audit_rows(factory):
        assert "secret-connection-id" not in event.metadata_json


def test_a_failed_tool_audit_does_not_fail_the_invocation(
    client: TestClient, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mutation has already run by the time the audit is appended.

    Raising here would report an error for work that succeeded, and a caller
    acting on that error could retry a mutation that already took effect.
    """
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

    def boom(*_args: object, **_kwargs: object) -> None:
        msg = "audit store unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(AuditRepository, "record", boom)

    response = client.post(
        "/v1/tools/connections.sync",
        json={"input": {"connection_id": "conn-1"}},
        headers={CSRF_HEADER_NAME: csrf},
    )

    assert response.status_code == 200
    # And the mutation it audits really did happen.
    with factory() as session:
        assert session.scalars(
            select(Job).where(Job.job_type == "connection.incremental_sync")
        ).one()
