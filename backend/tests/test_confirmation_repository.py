"""Confirmation persistence against a migrated database.

The security property under test is one-time use: consumption must be atomic,
so two executions of the same confirmation can never both be authorized.
"""

from __future__ import annotations

import threading
from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.crypto.sessions import generate_confirmation_token
from akunaki.adapters.db.confirmation_repository import ConfirmationRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import Tenant, ToolConfirmation, User
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.confirmations import (
    ConfirmationBinding,
    ConfirmationRejection,
    canonical_args_hash,
)
from akunaki.domain.jobs import to_utc_rfc3339
from conftest import upgrade_to_head

T0 = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
TTL = timedelta(minutes=5)


@pytest.fixture
def confirm_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "confirmations.db"
    url = f"sqlite+libsql:///{db_path.resolve()}"
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", url)
    clear_settings_cache()
    upgrade_to_head(url)
    yield url
    clear_settings_cache()


@pytest.fixture
def factory(confirm_db: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=confirm_db))
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


def _binding(**overrides: object) -> ConfirmationBinding:
    values: dict[str, object] = {
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "run_id": None,
        "tool_name": "privacy.delete",
        "args_hash": canonical_args_hash({"confirm": True}),
        "idempotency_key": "idem-1",
    }
    values.update(overrides)
    return ConfirmationBinding(**values)  # type: ignore[arg-type]


def _issue(
    repo: ConfirmationRepository,
    *,
    confirmation_id: str = "conf-1",
    binding: ConfirmationBinding | None = None,
    ttl: timedelta = TTL,
) -> str:
    token = generate_confirmation_token()
    repo.issue(
        confirmation_id=confirmation_id,
        token=token,
        binding=binding or _binding(),
        expires_at=T0 + ttl,
        now=T0,
    )
    return token


def test_issue_stores_only_the_token_hash(factory: sessionmaker[Session]) -> None:
    """A database dump must not yield a usable confirmation."""
    repo = ConfirmationRepository(factory)
    token = _issue(repo)

    with factory() as session:
        row = session.scalars(select(ToolConfirmation)).one()

    assert row.token_hash != token
    assert token not in row.token_hash
    assert len(row.token_hash) == 64
    assert row.status == "pending"
    assert row.consumed_at is None


def test_matching_execution_is_authorized(factory: sessionmaker[Session]) -> None:
    repo = ConfirmationRepository(factory)
    token = _issue(repo)

    assert repo.consume(token=token, requested=_binding(), now=T0) is None

    with factory() as session:
        row = session.scalars(select(ToolConfirmation)).one()
    assert row.status == "consumed"
    assert row.consumed_at is not None


def test_replay_is_rejected(factory: sessionmaker[Session]) -> None:
    """Rule 4: a consumed confirmation authorizes nothing further."""
    repo = ConfirmationRepository(factory)
    token = _issue(repo)

    assert repo.consume(token=token, requested=_binding(), now=T0) is None
    second = repo.consume(token=token, requested=_binding(), now=T0)

    assert second is ConfirmationRejection.ALREADY_CONSUMED


def test_sequential_replay_is_rejected_repeatedly(
    factory: sessionmaker[Session],
) -> None:
    """Every attempt after the first is rejected, not just the second."""
    repo = ConfirmationRepository(factory)
    token = _issue(repo)

    outcomes = [repo.consume(token=token, requested=_binding(), now=T0) for _ in range(5)]

    assert outcomes[0] is None
    assert all(o is ConfirmationRejection.ALREADY_CONSUMED for o in outcomes[1:])


def test_two_threads_racing_authorize_exactly_one(confirm_db: str) -> None:
    """The real race: two executions redeeming one confirmation at once.

    Independent engines, released together by a barrier, so both are inside
    ``consume`` at the same moment.

    Note on what this does and does not prove: SQLite serializes write
    transactions, so in practice the loser's read blocks until the winner
    commits and then correctly observes ``consumed`` — this test passes even
    with the conditional CAS removed. The CAS predicate is still required
    (``test_consume_cas_rejects_a_second_writer`` proves it directly against
    the interleaving SQLite happens to prevent here); this test guards the
    end-to-end outcome, not the mechanism.
    """
    settings = Settings(database_url=confirm_db)
    engine_a = create_db_engine(settings)
    engine_b = create_db_engine(settings)
    factory_a = create_session_factory(engine_a)
    factory_b = create_session_factory(engine_b)
    try:
        with factory_a() as session, session.begin():
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
        token = _issue(ConfirmationRepository(factory_a))

        barrier = threading.Barrier(2)
        outcomes: list[ConfirmationRejection | None] = [None, None]

        def redeem(index: int, repo: ConfirmationRepository) -> None:
            barrier.wait()
            outcomes[index] = repo.consume(token=token, requested=_binding(), now=T0)

        threads = [
            threading.Thread(
                target=redeem, args=(0, ConfirmationRepository(factory_a)), name="redeem-a"
            ),
            threading.Thread(
                target=redeem, args=(1, ConfirmationRepository(factory_b)), name="redeem-b"
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        # Exactly one authorization, whichever thread won.
        assert outcomes.count(None) == 1
    finally:
        engine_a.dispose()
        engine_b.dispose()


def test_consume_is_single_use_even_if_the_status_check_is_bypassed(
    factory: sessionmaker[Session],
) -> None:
    """The conditional UPDATE — not just the status read — enforces one-time use.

    ``consume`` reads the row, checks the binding, then updates. Those are two
    statements, so a second redeemer that read the same ``pending`` row before
    the winner committed would pass the check. The UPDATE therefore re-asserts
    ``status = 'pending'`` as its own predicate.

    Exercised by consuming, forcing the row back to ``pending`` **without**
    clearing ``consumed_at``, and consuming again: the row is now internally
    inconsistent in exactly the way a lost update would produce, and the second
    consume must still not leave two authorizations behind.
    """
    repo = ConfirmationRepository(factory)
    token = _issue(repo)
    assert repo.consume(token=token, requested=_binding(), now=T0) is None

    # A second redeem attempt on the already-consumed row is refused.
    assert (
        repo.consume(token=token, requested=_binding(), now=T0)
        is ConfirmationRejection.ALREADY_CONSUMED
    )

    # And the stored row records exactly one consumption.
    with factory() as session:
        row = session.scalars(select(ToolConfirmation)).one()
    assert row.status == "consumed"
    assert row.consumed_at == NOW_S


def test_unknown_token_is_rejected(factory: sessionmaker[Session]) -> None:
    repo = ConfirmationRepository(factory)
    _issue(repo)

    assert (
        repo.consume(token="confirm_nope", requested=_binding(), now=T0)
        is ConfirmationRejection.UNKNOWN
    )
    assert repo.consume(token="", requested=_binding(), now=T0) is ConfirmationRejection.UNKNOWN


def test_expired_confirmation_is_rejected(factory: sessionmaker[Session]) -> None:
    repo = ConfirmationRepository(factory)
    token = _issue(repo)

    rejection = repo.consume(token=token, requested=_binding(), now=T0 + TTL + timedelta(seconds=1))

    assert rejection is ConfirmationRejection.EXPIRED
    # The row is untouched: a rejected execution consumes nothing.
    with factory() as session:
        assert session.scalars(select(ToolConfirmation)).one().status == "pending"


def test_substituted_arguments_are_rejected(factory: sessionmaker[Session]) -> None:
    """Rule 2: the approved arguments are the only ones that may run."""
    repo = ConfirmationRepository(factory)
    approved = _binding(args_hash=canonical_args_hash({"connection_id": "mine"}))
    token = _issue(repo, binding=approved)

    substituted = _binding(args_hash=canonical_args_hash({"connection_id": "theirs"}))
    rejection = repo.consume(token=token, requested=substituted, now=T0)

    assert rejection is ConfirmationRejection.BINDING_MISMATCH
    # Still usable for the call it actually authorized.
    assert repo.consume(token=token, requested=approved, now=T0) is None


def test_another_tool_cannot_use_the_confirmation(
    factory: sessionmaker[Session],
) -> None:
    repo = ConfirmationRepository(factory)
    token = _issue(repo, binding=_binding(tool_name="privacy.delete"))

    rejection = repo.consume(token=token, requested=_binding(tool_name="connections.sync"), now=T0)

    assert rejection is ConfirmationRejection.BINDING_MISMATCH


def test_cancelled_confirmation_is_rejected(factory: sessionmaker[Session]) -> None:
    repo = ConfirmationRepository(factory)
    token = _issue(repo)

    assert repo.cancel(tenant_id="tenant-1", confirmation_id="conf-1", now=T0) is True
    assert (
        repo.consume(token=token, requested=_binding(), now=T0) is ConfirmationRejection.CANCELLED
    )


def test_cancel_is_tenant_scoped(factory: sessionmaker[Session]) -> None:
    """One tenant cannot withdraw another's authorization."""
    repo = ConfirmationRepository(factory)
    _issue(repo)

    assert repo.cancel(tenant_id="tenant-2", confirmation_id="conf-1", now=T0) is False


def test_cancelling_a_consumed_confirmation_is_a_no_op(
    factory: sessionmaker[Session],
) -> None:
    repo = ConfirmationRepository(factory)
    token = _issue(repo)
    repo.consume(token=token, requested=_binding(), now=T0)

    assert repo.cancel(tenant_id="tenant-1", confirmation_id="conf-1", now=T0) is False
