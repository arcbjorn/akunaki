"""The scheduled expiry sweep for short-lived credential material.

Sessions, OAuth/PKCE states, login states, and confirmations all expire. Until
this shipped nothing deleted them, so every expired row kept its stored secrets
forever. The properties that matter: live rows survive, expired ones do not,
and one broken store does not stop the rest.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.crypto.sessions import generate_confirmation_token
from akunaki.adapters.db.confirmation_repository import ConfirmationRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.job_repository import JobRepository
from akunaki.adapters.db.models import SessionRow, Tenant, ToolConfirmation, User
from akunaki.adapters.db.session_repository import SessionRepository
from akunaki.application.handlers import HandlerRegistry
from akunaki.application.retention_handlers import RetentionSweepHandler
from akunaki.application.worker_runtime import JobWorker, WorkerConfig
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.confirmations import ConfirmationBinding, canonical_args_hash
from akunaki.domain.jobs import RETENTION_SWEEP_JOB_TYPE, JobClaim, JobRole, to_utc_rfc3339
from akunaki.domain.tenants import SYSTEM_TENANT_ID

T0 = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
TTL = timedelta(hours=1)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def sweep_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "retention.db"
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
def factory(sweep_db: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=sweep_db))
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


def _claim() -> JobClaim:
    return JobClaim(
        job_id="sweep-1",
        tenant_id="system",
        role=JobRole.CORE,
        job_type=RETENTION_SWEEP_JOB_TYPE,
        owner="worker-1",
        fence_token=1,
        leased_until=NOW_S,
        attempts=1,
        max_attempts=5,
        payload_json="{}",
    )


def _issue_session(factory: sessionmaker[Session], *, session_id: str, ttl: timedelta) -> None:
    SessionRepository(factory).issue(
        session_id=session_id,
        user_id="user-1",
        now=T0,
        ttl=ttl,
    )


def _issue_confirmation(
    factory: sessionmaker[Session], *, confirmation_id: str, ttl: timedelta
) -> None:
    ConfirmationRepository(factory).issue(
        confirmation_id=confirmation_id,
        token=generate_confirmation_token(),
        binding=ConfirmationBinding(
            tenant_id="tenant-1",
            user_id="user-1",
            run_id=None,
            tool_name="privacy.delete",
            args_hash=canonical_args_hash({}),
            idempotency_key=confirmation_id,
        ),
        expires_at=T0 + ttl,
        now=T0,
    )


def _sweep(factory: sessionmaker[Session], *, at: datetime) -> None:
    RetentionSweepHandler(
        stores={
            "sessions": SessionRepository(factory),
            "tool_confirmations": ConfirmationRepository(factory),
        },
        clock=lambda: at,
    )(_claim())


def test_expired_rows_are_removed(factory: sessionmaker[Session]) -> None:
    """An expired row's stored secrets must not outlive the window."""
    _issue_session(factory, session_id="sess-old", ttl=TTL)
    _issue_confirmation(factory, confirmation_id="conf-old", ttl=TTL)

    _sweep(factory, at=T0 + TTL + timedelta(minutes=1))

    with factory() as session:
        assert session.scalars(select(SessionRow)).all() == []
        assert session.scalars(select(ToolConfirmation)).all() == []


def test_live_rows_survive(factory: sessionmaker[Session]) -> None:
    """The sweep must never remove something still usable.

    This is the property that makes the job safe to run unattended: it deletes
    on expiry alone, so a live session cannot be swept out from under a user.
    """
    _issue_session(factory, session_id="sess-live", ttl=timedelta(hours=12))
    _issue_confirmation(factory, confirmation_id="conf-live", ttl=timedelta(hours=12))

    _sweep(factory, at=T0 + TTL + timedelta(minutes=1))

    with factory() as session:
        assert len(session.scalars(select(SessionRow)).all()) == 1
        assert len(session.scalars(select(ToolConfirmation)).all()) == 1


def test_sweep_removes_only_the_expired_ones(factory: sessionmaker[Session]) -> None:
    _issue_session(factory, session_id="sess-old", ttl=TTL)
    _issue_session(factory, session_id="sess-live", ttl=timedelta(hours=12))

    _sweep(factory, at=T0 + TTL + timedelta(minutes=1))

    with factory() as session:
        remaining = [row.id for row in session.scalars(select(SessionRow))]
    assert remaining == ["sess-live"]


def test_a_consumed_confirmation_is_kept_until_it_expires(
    factory: sessionmaker[Session],
) -> None:
    """Replay defence outlives use, but not expiry.

    A consumed confirmation's job is to make a replay fail; once expired it
    fails on expiry alone, so keeping it only retains hashes for a call that
    can never run again.
    """
    _issue_confirmation(factory, confirmation_id="conf-1", ttl=timedelta(hours=12))
    with factory() as session, session.begin():
        row = session.scalars(select(ToolConfirmation)).one()
        row.status = "consumed"
        row.consumed_at = NOW_S

    _sweep(factory, at=T0 + TTL)
    with factory() as session:
        assert len(session.scalars(select(ToolConfirmation)).all()) == 1

    _sweep(factory, at=T0 + timedelta(hours=13))
    with factory() as session:
        assert session.scalars(select(ToolConfirmation)).all() == []


def test_empty_stores_are_a_no_op(factory: sessionmaker[Session]) -> None:
    _sweep(factory, at=T0 + TTL)  # must not raise

    with factory() as session:
        assert session.scalars(select(SessionRow)).all() == []


# ---------------------------------------------------------------------------
# One failing store must not stop the others
# ---------------------------------------------------------------------------


class _BrokenStore:
    def purge_expired(self, *, now: datetime) -> int:
        msg = "store unavailable"
        raise RuntimeError(msg)


class _CountingStore:
    def __init__(self) -> None:
        self.calls = 0

    def purge_expired(self, *, now: datetime) -> int:
        self.calls += 1
        return 3


def test_a_broken_store_does_not_block_the_rest() -> None:
    """Aborting halfway would leave the other stores un-purged until next tick."""
    healthy = _CountingStore()
    handler = RetentionSweepHandler(
        stores={"broken": _BrokenStore(), "healthy": healthy},
        clock=lambda: T0,
    )

    with pytest.raises(RuntimeError, match="broken"):
        handler(_claim())

    # "broken" sorts before "healthy", so the failure came first and the
    # healthy store was still swept.
    assert healthy.calls == 1


def test_an_all_healthy_sweep_does_not_raise() -> None:
    handler = RetentionSweepHandler(
        stores={"a": _CountingStore(), "b": _CountingStore()},
        clock=lambda: T0,
    )

    handler(_claim())  # must not raise


def test_sweep_runs_through_the_real_worker(factory: sessionmaker[Session]) -> None:
    """The wiring, not just the handler: enqueue -> claim -> purge.

    A purge method nobody calls is exactly what this replaces, so the test that
    matters is the one proving the job actually executes.
    """
    _issue_session(factory, session_id="sess-old", ttl=TTL)
    later = T0 + TTL + timedelta(minutes=1)

    JobRepository(factory).enqueue_job(
        job_id="retention-1",
        tenant_id=SYSTEM_TENANT_ID,
        job_type=RETENTION_SWEEP_JOB_TYPE,
        payload_json="{}",
        now=later,
    )
    worker = JobWorker(
        JobRepository(factory),
        owner="worker-1",
        config=WorkerConfig(lease_ttl=timedelta(seconds=60)),
        registry=HandlerRegistry(
            {
                RETENTION_SWEEP_JOB_TYPE: RetentionSweepHandler(
                    stores={"sessions": SessionRepository(factory)},
                    clock=lambda: later,
                )
            }
        ),
        clock=lambda: later,
        sleep=lambda _s: None,
        jitter=lambda: 0.0,
    )
    worker.run_once()

    assert worker.stats.succeeded == 1
    with factory() as session:
        assert session.scalars(select(SessionRow)).all() == []
