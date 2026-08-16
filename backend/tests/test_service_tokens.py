"""Service tokens: hash-only persistence and the bearer path over ``/v1/tools``.

The repository half proves the credential lifecycle (issue once, validate,
expire, revoke) and that the raw token never lands in the database. The route
half proves the one place Bearer is accepted: the tools surface — reads work
without a cookie or CSRF, what a mutation needs follows the tool's declared
confirmation policy and the token's scope, and every other ``/v1`` route still
demands a session.
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

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import (
    AuditEventRow,
    Connection,
    FactRecord,
    Job,
    ServiceToken,
    SleepSession,
    Tenant,
    User,
)
from akunaki.adapters.db.service_token_repository import ServiceTokenRepository
from akunaki.api.app import create_app
from akunaki.api.routes.tools import _token_may_invoke
from akunaki.application.tool_registry import ConfirmationPolicy, SideEffect, Tool
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339
from akunaki.domain.service_tokens import (
    AuthenticatedServiceToken,
    ServiceTokenRejection,
    ServiceTokenScope,
)
from conftest import upgrade_to_head

T0 = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
DAY = "2026-08-15"


@pytest.fixture
def route_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "service_tokens.db"
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


def _issue(
    factory: sessionmaker[Session],
    *,
    ttl: timedelta | None = None,
    scope: ServiceTokenScope = ServiceTokenScope.READ,
    row_id: str = "tok-1",
) -> str:
    issued = ServiceTokenRepository(factory).issue(
        token_id=row_id, user_id="user-1", name="odin-personal", now=T0, scope=scope, ttl=ttl
    )
    return issued.token


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Repository lifecycle
# ---------------------------------------------------------------------------


def test_issue_stores_the_hash_never_the_token(factory: sessionmaker[Session]) -> None:
    token = _issue(factory)
    assert token.startswith("aksvc_")
    with factory() as session:
        row = session.scalars(select(ServiceToken)).one()
    assert row.token_hash != token
    assert token not in (row.token_hash, row.id, row.name)
    assert row.scope == "read"
    assert row.expires_at is None


def test_validate_accepts_a_live_token(factory: sessionmaker[Session]) -> None:
    token = _issue(factory)
    result = ServiceTokenRepository(factory).validate(token=token, now=T0)
    assert result.ok and result.principal is not None
    assert result.principal.tenant_id == "tenant-1"
    assert result.principal.user_id == "user-1"
    assert result.principal.scope is ServiceTokenScope.READ


def test_validate_rejects_unknown_expired_and_revoked(factory: sessionmaker[Session]) -> None:
    repo = ServiceTokenRepository(factory)

    assert repo.validate(token="aksvc_nope", now=T0).rejection is ServiceTokenRejection.NOT_FOUND

    expiring = _issue(factory, ttl=timedelta(hours=1))
    late = T0 + timedelta(hours=2)
    assert repo.validate(token=expiring, now=late).rejection is ServiceTokenRejection.EXPIRED

    assert repo.revoke(token_id="tok-1", now=T0) is True
    assert repo.validate(token=expiring, now=T0).rejection is ServiceTokenRejection.REVOKED
    # Revoking again is a no-op, not an error.
    assert repo.revoke(token_id="tok-1", now=T0) is False


def test_the_wider_scope_round_trips_through_storage(factory: sessionmaker[Session]) -> None:
    """Stored and rehydrated, so validation reports the grant that was minted."""
    token = _issue(factory, scope=ServiceTokenScope.READ_SYNC)

    with factory() as session:
        assert session.scalars(select(ServiceToken)).one().scope == "read_sync"
    principal = ServiceTokenRepository(factory).validate(token=token, now=T0).principal
    assert principal is not None
    assert principal.scope is ServiceTokenScope.READ_SYNC
    assert principal.scope.may_invoke_if_agent is True


def test_read_is_the_default_scope(factory: sessionmaker[Session]) -> None:
    """An omitted scope must grant the *narrower* authority, never the wider."""
    issued = ServiceTokenRepository(factory).issue(
        token_id="tok-default", user_id="user-1", name="unspecified", now=T0
    )
    assert issued.scope is ServiceTokenScope.READ
    assert issued.scope.may_invoke_if_agent is False


def test_issue_validates_its_inputs(factory: sessionmaker[Session]) -> None:
    repo = ServiceTokenRepository(factory)
    with pytest.raises(ValueError, match="must be non-empty"):
        repo.issue(token_id="", user_id="user-1", name="x", now=T0)
    with pytest.raises(ValueError, match="at least one second"):
        repo.issue(token_id="t", user_id="user-1", name="x", now=T0, ttl=timedelta(0))
    with pytest.raises(ValueError, match="not found"):
        repo.issue(token_id="t", user_id="nobody", name="x", now=T0)


def test_list_for_tenant_names_tokens_without_secrets(factory: sessionmaker[Session]) -> None:
    _issue(factory)
    rows = ServiceTokenRepository(factory).list_for_tenant(tenant_id="tenant-1")
    assert rows == [("tok-1", "odin-personal", "read", None)]


# ---------------------------------------------------------------------------
# Bearer over /v1/tools
# ---------------------------------------------------------------------------


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


def test_bearer_lists_tools_without_a_cookie(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    token = _issue(factory)
    response = client.get("/v1/tools", headers=_bearer(token))
    assert response.status_code == 200
    names = {t["name"] for t in response.json()["tools"]}
    assert "health.get_today" in names


def test_bearer_invokes_a_read_tool_without_csrf(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    _seed_sleep(factory, day=DAY, fact_id="s1")
    token = _issue(factory)
    response = client.post(
        "/v1/tools/health.get_sleep", headers=_bearer(token), json={"input": {"day": DAY}}
    )
    assert response.status_code == 200
    assert response.json()["local_health_day"] == DAY


def _principal(scope: ServiceTokenScope) -> AuthenticatedServiceToken:
    return AuthenticatedServiceToken(
        token_id="tok-1",
        tenant_id="tenant-1",
        user_id="user-1",
        scope=scope,
        expires_at=None,
    )


def _tool(
    *,
    confirmation: ConfirmationPolicy,
    side_effect: SideEffect = SideEffect.NONE,
) -> Tool[BaseModel, BaseModel]:
    return Tool(
        name="test.tool",
        input_model=BaseModel,
        output_model=BaseModel,
        handler=lambda _inputs, _context: BaseModel(),
        side_effect=side_effect,
        confirmation=confirmation,
    )


@pytest.mark.parametrize(
    ("confirmation", "side_effect", "read_ok", "read_sync_ok"),
    [
        # A read: both scopes.
        (ConfirmationPolicy.NEVER, SideEffect.NONE, True, True),
        # An "if agent" mutation: only the scope that opted in.
        (ConfirmationPolicy.IF_AGENT, SideEffect.ENQUEUE_JOB, False, True),
        # Destructive: neither scope, ever.
        (ConfirmationPolicy.ALWAYS, SideEffect.DESTROY_DATA, False, False),
        # Degenerate: a side effect nothing gates. Not reachable through the
        # shipped registry, but if a tool were ever registered that way it must
        # fail closed rather than become a mutation any token can invoke.
        (ConfirmationPolicy.NEVER, SideEffect.ENQUEUE_JOB, False, False),
    ],
)
def test_scope_admits_exactly_the_declared_policies(
    confirmation: ConfirmationPolicy,
    side_effect: SideEffect,
    read_ok: bool,
    read_sync_ok: bool,
) -> None:
    """The whole matrix in one place: what each scope may reach, and why.

    Pinned as a table because the rule is a security boundary, and a change to
    any single cell should have to be written down deliberately.
    """
    tool = _tool(confirmation=confirmation, side_effect=side_effect)

    assert _token_may_invoke(_principal(ServiceTokenScope.READ), tool) is read_ok
    assert _token_may_invoke(_principal(ServiceTokenScope.READ_SYNC), tool) is read_sync_ok


def _link_connection(factory: sessionmaker[Session], *, connection_id: str = "conn-1") -> None:
    with factory() as session, session.begin():
        session.add(
            Connection(
                id=connection_id,
                tenant_id="tenant-1",
                provider="polar",
                status="active",
                scopes_granted_json="[]",
                external_user_id=None,
                connected_at=NOW_S,
                updated_at=NOW_S,
            )
        )


def test_a_read_token_cannot_invoke_a_mutating_tool(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """The narrow scope keeps exactly its old behaviour.

    ``connections.sync`` is ``IF_AGENT``, so reaching it needs a scope that
    opted in. A token minted before ``read_sync`` existed did not, and must not
    gain the capability retroactively.
    """
    _link_connection(factory)
    token = _issue(factory)

    response = client.post(
        "/v1/tools/connections.sync",
        headers=_bearer(token),
        json={"input": {"connection_id": "conn-1"}},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "forbidden"
    with factory() as session:
        assert session.scalars(select(Job)).all() == []


def test_a_read_sync_token_enqueues_a_sync(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """The point of the wider scope: an agent can ask for fresh data.

    The enqueue is the same ``connection.incremental_sync`` job a webhook or
    the reconcile sweep would queue — an agent cannot reach a sync path a user
    could not — and it is idempotent, so a retry collapses rather than piling up.
    """
    _link_connection(factory)
    token = _issue(factory, scope=ServiceTokenScope.READ_SYNC)

    response = client.post(
        "/v1/tools/connections.sync",
        headers=_bearer(token),
        json={"input": {"connection_id": "conn-1"}},
    )

    assert response.status_code == 200
    assert response.json()["created"] is True
    with factory() as session:
        job = session.scalars(
            select(Job).where(Job.job_type == "connection.incremental_sync")
        ).one()
    assert job.tenant_id == "tenant-1"


def test_a_repeated_sync_from_a_token_does_not_pile_up_jobs(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """Idempotent by design: this is why the enqueue is safe to expose at all."""
    _link_connection(factory)
    token = _issue(factory, scope=ServiceTokenScope.READ_SYNC)
    payload = {"input": {"connection_id": "conn-1"}}

    first = client.post("/v1/tools/connections.sync", headers=_bearer(token), json=payload)
    second = client.post("/v1/tools/connections.sync", headers=_bearer(token), json=payload)

    assert first.json()["created"] is True
    # Deduplicated onto the in-flight job rather than queued twice.
    assert second.json()["created"] is False
    assert first.json()["job_id"] == second.json()["job_id"]
    with factory() as session:
        assert len(session.scalars(select(Job)).all()) == 1


def test_no_scope_can_reach_a_destructive_tool(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """``ALWAYS`` is refused for every service token, however it was minted.

    This is the line the whole change is drawn around: widening the scope
    admits an idempotent enqueue, never an irreversible erasure. A bearer
    credential must not be able to destroy data even holding a confirmation.
    """
    for scope, row_id in (
        (ServiceTokenScope.READ, "tok-read"),
        (ServiceTokenScope.READ_SYNC, "tok-sync"),
    ):
        token = _issue(factory, scope=scope, row_id=row_id)
        response = client.post(
            "/v1/tools/privacy.delete", headers=_bearer(token), json={"input": {}}
        )
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "forbidden"

    # And the tenant it would have erased is still there.
    with factory() as session:
        assert session.get(Tenant, "tenant-1") is not None


def test_a_token_still_cannot_sync_another_tenants_connection(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """Scope widens *which tools*, never *whose data*."""
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
    token = _issue(factory, scope=ServiceTokenScope.READ_SYNC)

    response = client.post(
        "/v1/tools/connections.sync",
        headers=_bearer(token),
        json={"input": {"connection_id": "conn-theirs"}},
    )

    assert response.status_code == 404
    with factory() as session:
        assert session.scalars(select(Job)).all() == []


def test_a_token_sync_carries_its_origin_into_the_audit_trail(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """A reviewer must be able to tell which credential asked for the sync.

    A service token acts *for* the user but is not the user at a browser, and
    now that it can cause an enqueue, the distinction is what makes the trail
    worth reading.
    """
    _link_connection(factory)
    token = _issue(factory, scope=ServiceTokenScope.READ_SYNC)

    client.post(
        "/v1/tools/connections.sync",
        headers=_bearer(token),
        json={"input": {"connection_id": "conn-1"}},
    )

    with factory() as session:
        [event] = list(session.scalars(select(AuditEventRow)))
    assert event.resource_id == "connections.sync"
    assert json.loads(event.metadata_json) == {
        "outcome": "succeeded",
        "origin": "service_token",
    }


def test_a_refused_token_mutation_is_audited(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """A refusal is what a stolen credential looks like from the outside."""
    token = _issue(factory)

    client.post(
        "/v1/tools/connections.sync",
        headers=_bearer(token),
        json={"input": {"connection_id": "conn-1"}},
    )

    with factory() as session:
        [event] = list(session.scalars(select(AuditEventRow)))
    assert json.loads(event.metadata_json) == {
        "outcome": "refused",
        "origin": "service_token",
    }


def test_an_agent_run_call_from_a_token_still_needs_a_confirmation(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """Scope admits the tool; the policy still decides what the *call* needs.

    ``IF_AGENT`` means a call carrying a ``run_id`` must prove the user
    authorized that exact call. Widening the scope does not exempt a token from
    that — it only gets it past the door.
    """
    _link_connection(factory)
    token = _issue(factory, scope=ServiceTokenScope.READ_SYNC)

    response = client.post(
        "/v1/tools/connections.sync",
        headers=_bearer(token),
        json={"input": {"connection_id": "conn-1"}, "run_id": "run-1"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "confirmation_required"
    with factory() as session:
        assert session.scalars(select(Job)).all() == []


def test_bearer_rejections_are_one_generic_401(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    expiring = _issue(factory, ttl=timedelta(hours=1))
    ServiceTokenRepository(factory).revoke(token_id="tok-1", now=datetime.now(UTC))

    for headers in (
        _bearer("aksvc_unknown"),
        _bearer(expiring),  # revoked
        {"Authorization": "Basic dXNlcjpwdw=="},  # wrong scheme
        {"Authorization": "Bearer "},  # empty credential
    ):
        assert client.get("/v1/tools", headers=headers).status_code == 401


def test_a_bearer_header_is_never_downgraded_to_the_cookie(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    # A valid cookie session plus a bad bearer token must fail: the explicit
    # credential decides, or a stolen header could ride ambient cookie auth.
    from akunaki.adapters.db.session_repository import SessionRepository
    from akunaki.api.security import SESSION_COOKIE_NAME

    issued = SessionRepository(factory).issue(
        session_id="sess-1", user_id="user-1", now=datetime.now(UTC), ttl=timedelta(hours=1)
    )
    client.cookies.set(SESSION_COOKIE_NAME, issued.token)
    assert client.get("/v1/tools", headers=_bearer("aksvc_bad")).status_code == 401


def test_bearer_does_not_open_the_rest_of_v1(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    # The token is scoped to the tools surface; the product routes stay
    # session-only until Bearer is deliberately extended to them.
    token = _issue(factory)
    assert client.get("/v1/today", headers=_bearer(token)).status_code == 401
    assert client.get("/v1/recovery", headers=_bearer(token)).status_code == 401
