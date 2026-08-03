"""Integration tests: fenced unit of work over a real file-backed libSQL DB.

The unit of work exists to close a time-of-check/time-of-use window: checking
``has_valid_job_lease`` and *then* writing lets the lease expire in the gap, so
a superseded worker's side effect lands behind the worker that took the job
over. These tests prove the check and the write share one transaction, so a
lost lease rolls the side effect back instead of committing it.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.job_repository import JobRepository
from akunaki.adapters.db.models import Job, Tenant
from akunaki.adapters.db.unit_of_work import FencedUnitOfWork
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import JobRole, to_utc_rfc3339
from akunaki.ports.unit_of_work import LeaseLostError

T0 = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
LEASE_TTL = timedelta(seconds=30)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(_backend_root() / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.set_main_option("script_location", str(_backend_root() / "src" / "akunaki" / "migrations"))
    return cfg


@pytest.fixture
def uow_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "uow.db"
    url = f"sqlite+libsql:///{db_path.resolve()}"
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", url)
    clear_settings_cache()
    command.upgrade(_alembic_config(url), "head")
    yield url
    clear_settings_cache()


def _factory(database_url: str) -> tuple[sessionmaker[Session], object]:
    engine = create_db_engine(Settings(database_url=database_url))
    return create_session_factory(engine), engine


def _seed(factory: sessionmaker[Session], *, job_id: str) -> None:
    with factory() as session, session.begin():
        session.add(
            Tenant(
                id="tenant-1",
                created_at=to_utc_rfc3339(T0),
                status="active",
                primary_timezone="UTC",
                display_name="Test",
            )
        )
        session.add(
            Job(
                id=job_id,
                tenant_id="tenant-1",
                role="core",
                status="ready",
                payload_json='{"kind":"ping"}',
                priority=100,
                run_after=to_utc_rfc3339(T0),
                attempts=0,
                max_attempts=5,
                idempotency_key=job_id,
                fence_token=0,
                created_at=to_utc_rfc3339(T0),
                updated_at=to_utc_rfc3339(T0),
                job_type="system.noop",
            )
        )


def _mark(session: Session, *, job_id: str, label: str) -> str:
    """A stand-in domain side effect: an observable write on the job row."""
    job = session.get(Job, job_id)
    assert job is not None
    job.payload_json = json.dumps({"by": label})
    return label


def _payload_of(factory: sessionmaker[Session], job_id: str) -> str:
    """Return the marker a side effect wrote, or ``"unwritten"`` if none did."""
    with factory() as session:
        value = session.execute(select(Job.payload_json).where(Job.id == job_id)).scalar_one()
    parsed = json.loads(str(value))
    return str(parsed.get("by", "unwritten"))


def test_side_effect_commits_while_lease_is_held(uow_db: str) -> None:
    """The happy path: work commits atomically with a valid fence."""
    factory, engine = _factory(uow_db)
    try:
        _seed(factory, job_id="job-ok")
        repo = JobRepository(factory)
        claim = repo.claim_next(role=JobRole.CORE, owner="worker-a", lease_ttl=LEASE_TTL, now=T0)
        assert claim is not None

        uow = FencedUnitOfWork(factory)
        result = uow.run_fenced(
            claim,
            lambda session: _mark(session, job_id=claim.job_id, label="written-by-a"),
            now=T0 + timedelta(seconds=1),
        )

        assert result == "written-by-a"
        assert _payload_of(factory, "job-ok") == "written-by-a"
    finally:
        engine.dispose()  # type: ignore[attr-defined]


def test_expired_lease_rolls_the_side_effect_back(uow_db: str) -> None:
    """An expired lease must leave **no** trace of the work."""
    factory, engine = _factory(uow_db)
    try:
        _seed(factory, job_id="job-expired")
        repo = JobRepository(factory)
        claim = repo.claim_next(role=JobRole.CORE, owner="worker-a", lease_ttl=LEASE_TTL, now=T0)
        assert claim is not None

        uow = FencedUnitOfWork(factory)
        with pytest.raises(LeaseLostError):
            uow.run_fenced(
                claim,
                lambda session: _mark(session, job_id=claim.job_id, label="stale-write"),
                # Past the lease expiry.
                now=T0 + LEASE_TTL + timedelta(seconds=1),
            )

        assert _payload_of(factory, "job-expired") == "unwritten"
    finally:
        engine.dispose()  # type: ignore[attr-defined]


def test_stale_worker_cannot_write_behind_the_new_owner(uow_db: str) -> None:
    """The corruption this exists to prevent, end to end.

    Worker A's lease expires mid-execution; the job is requeued and worker B
    claims it at a higher fence. A's side effect must roll back rather than
    landing on top of B's.
    """
    factory, engine = _factory(uow_db)
    try:
        _seed(factory, job_id="job-stolen")
        repo = JobRepository(factory)
        claim_a = repo.claim_next(role=JobRole.CORE, owner="worker-a", lease_ttl=LEASE_TTL, now=T0)
        assert claim_a is not None

        # A's lease expires and the reaper requeues the job.
        after_expiry = T0 + LEASE_TTL + timedelta(seconds=1)
        requeued = repo.requeue_expired_leases(now=after_expiry)
        assert requeued >= 1

        # B claims it at a strictly higher fence and does its work.
        claim_b = repo.claim_next(
            role=JobRole.CORE, owner="worker-b", lease_ttl=LEASE_TTL, now=after_expiry
        )
        assert claim_b is not None
        assert claim_b.fence_token > claim_a.fence_token

        uow = FencedUnitOfWork(factory)
        uow.run_fenced(
            claim_b,
            lambda session: _mark(session, job_id="job-stolen", label="written-by-b"),
            now=after_expiry + timedelta(seconds=1),
        )

        # A, still running, now tries to persist its stale computation.
        with pytest.raises(LeaseLostError):
            uow.run_fenced(
                claim_a,
                lambda session: _mark(session, job_id="job-stolen", label="written-by-a"),
                now=after_expiry + timedelta(seconds=2),
            )

        # B's value survives; A's never landed.
        assert _payload_of(factory, "job-stolen") == "written-by-b"
    finally:
        engine.dispose()  # type: ignore[attr-defined]


def test_lease_lost_during_work_rolls_back(uow_db: str) -> None:
    """The post-check is load-bearing: expiry *during* ``work`` still rolls back.

    The pre-check passes, then the lease is stolen while ``work`` runs. Only a
    check that shares the write transaction can catch this.
    """
    factory, engine = _factory(uow_db)
    try:
        _seed(factory, job_id="job-midflight")
        repo = JobRepository(factory)
        claim = repo.claim_next(role=JobRole.CORE, owner="worker-a", lease_ttl=LEASE_TTL, now=T0)
        assert claim is not None

        after_expiry = T0 + LEASE_TTL + timedelta(seconds=1)

        def work_then_lose_lease(session: Session) -> str:
            # The write happens while the fence is still valid...
            _mark(session, job_id="job-midflight", label="stale-write")
            # ...and the lease is taken over before this transaction commits.
            repo.requeue_expired_leases(now=after_expiry)
            other = repo.claim_next(
                role=JobRole.CORE, owner="worker-b", lease_ttl=LEASE_TTL, now=after_expiry
            )
            assert other is not None
            return "stale-write"

        with pytest.raises(LeaseLostError):
            FencedUnitOfWork(factory).run_fenced(
                claim,
                work_then_lose_lease,
                now=T0 + timedelta(seconds=1),
            )

        assert _payload_of(factory, "job-midflight") == "unwritten"
    finally:
        engine.dispose()  # type: ignore[attr-defined]


def test_work_exceptions_propagate_and_roll_back(uow_db: str) -> None:
    """A failing handler rolls back and surfaces its own error, not LeaseLostError."""
    factory, engine = _factory(uow_db)
    try:
        _seed(factory, job_id="job-raises")
        repo = JobRepository(factory)
        claim = repo.claim_next(role=JobRole.CORE, owner="worker-a", lease_ttl=LEASE_TTL, now=T0)
        assert claim is not None

        def boom(session: Session) -> str:
            _mark(session, job_id="job-raises", label="partial")
            msg = "handler failed"
            raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="handler failed"):
            FencedUnitOfWork(factory).run_fenced(claim, boom, now=T0 + timedelta(seconds=1))

        assert _payload_of(factory, "job-raises") == "unwritten"
    finally:
        engine.dispose()  # type: ignore[attr-defined]
