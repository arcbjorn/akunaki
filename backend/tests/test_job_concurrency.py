"""Integration tests: durable job CAS claim, leases, and leader fencing.

Uses separate engines/session factories as independent clients on a local
file-backed libSQL database under tmp_path. Concurrency is deterministic and
bounded (fixed job counts, barriers). No FOR UPDATE / SKIP LOCKED.
"""

from __future__ import annotations

import threading
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, create_engine, event, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import Table

from akunaki.adapters.db.engine import (
    BUSY_TIMEOUT_MS,
    create_db_engine,
    create_session_factory,
    probe_database_ready,
)
from akunaki.adapters.db.job_repository import MIN_LEASE_TTL, JobRepository
from akunaki.adapters.db.models import Job, JobAttempt, JobDeadLetter, JobLease, LeaderLease, Tenant
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import (
    JobCandidate,
    JobClaim,
    JobRole,
    JobStatus,
    LeaderClaim,
    require_aware,
    to_utc_rfc3339,
)
from conftest import head_revision

T0 = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)
LEASE_TTL = timedelta(seconds=30)

# sqlalchemy-libsql surfaces some SQLite constraint failures as ValueError.
ConstraintError = (IntegrityError, ValueError)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(_backend_root() / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.set_main_option("script_location", str(_backend_root() / "alembic"))
    return cfg


def _migrate(database_url: str) -> None:
    clear_settings_cache()
    command.upgrade(_alembic_config(database_url), "head")


def _client_pair(
    database_url: str,
) -> tuple[sessionmaker[Session], sessionmaker[Session], Engine, Engine]:
    """Two independent engines/session factories against the same file DB."""
    settings = Settings(database_url=database_url)
    engine_a = create_db_engine(settings)
    engine_b = create_db_engine(settings)
    return (
        create_session_factory(engine_a),
        create_session_factory(engine_b),
        engine_a,
        engine_b,
    )


def _seed_tenant(session_factory: sessionmaker[Session], tenant_id: str = "tenant-1") -> None:
    with session_factory() as session, session.begin():
        session.add(
            Tenant(
                id=tenant_id,
                created_at=to_utc_rfc3339(T0),
                status="active",
                primary_timezone="UTC",
                display_name="Test",
            )
        )


def _add_job(
    session_factory: sessionmaker[Session],
    *,
    job_id: str,
    tenant_id: str = "tenant-1",
    role: str = "core",
    status: str = "ready",
    priority: int = 100,
    run_after: datetime = T0,
    attempts: int = 0,
    max_attempts: int = 5,
    fence_token: int = 0,
    created_at: datetime | None = None,
    payload: str = '{"kind":"ping"}',
    idempotency_key: str | None = None,
    job_type: str = "system.noop",
) -> None:
    created = created_at if created_at is not None else run_after
    with session_factory() as session, session.begin():
        session.add(
            Job(
                id=job_id,
                tenant_id=tenant_id,
                role=role,
                status=status,
                payload_json=payload,
                priority=priority,
                run_after=to_utc_rfc3339(run_after),
                attempts=attempts,
                max_attempts=max_attempts,
                idempotency_key=idempotency_key or job_id,
                fence_token=fence_token,
                created_at=to_utc_rfc3339(created),
                updated_at=to_utc_rfc3339(created),
                job_type=job_type,
            )
        )


@pytest.fixture
def concurrency_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "concurrency.db"
    url = f"sqlite+libsql:///{db_path.resolve()}"
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", url)
    clear_settings_cache()
    _migrate(url)
    yield url
    clear_settings_cache()


# ---------------------------------------------------------------------------
# Domain time helpers
# ---------------------------------------------------------------------------


def test_to_utc_rfc3339_rejects_naive() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        to_utc_rfc3339(datetime(2026, 7, 13, 12, 0, 0))


def test_require_aware_rejects_naive() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        require_aware(datetime(2026, 7, 13, 12, 0, 0))


def test_to_utc_rfc3339_sortable_and_zulu() -> None:
    earlier = to_utc_rfc3339(datetime(2026, 7, 13, 11, 0, 0, tzinfo=UTC))
    later = to_utc_rfc3339(datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC))
    assert earlier < later
    assert earlier.endswith("Z")
    assert later.endswith("Z")


def test_to_utc_rfc3339_second_resolution_truncates_subseconds() -> None:
    """Canonical representation is second precision (microseconds dropped)."""
    with_frac = datetime(2026, 7, 13, 12, 0, 0, 999_999, tzinfo=UTC)
    assert to_utc_rfc3339(with_frac) == "2026-07-13T12:00:00Z"
    assert timedelta(seconds=1) == MIN_LEASE_TTL


# ---------------------------------------------------------------------------
# Discovery ordering and filters
# ---------------------------------------------------------------------------


def test_discover_due_ordering_and_filters(concurrency_db: str) -> None:
    factory, _, engine, _ = _client_pair(concurrency_db)
    try:
        _seed_tenant(factory)
        # Lower priority number wins; then earlier created_at.
        _add_job(
            factory,
            job_id="future",
            priority=1,
            run_after=T0 + timedelta(hours=1),
            created_at=T0,
        )
        _add_job(
            factory,
            job_id="agent-only",
            role="agent",
            priority=1,
            run_after=T0 - timedelta(minutes=1),
            created_at=T0,
        )
        _add_job(
            factory,
            job_id="low-pri",
            priority=50,
            run_after=T0 - timedelta(minutes=5),
            created_at=T0 + timedelta(seconds=2),
        )
        _add_job(
            factory,
            job_id="high-pri-late",
            priority=10,
            run_after=T0 - timedelta(minutes=5),
            created_at=T0 + timedelta(seconds=5),
        )
        _add_job(
            factory,
            job_id="high-pri-early",
            priority=10,
            run_after=T0 - timedelta(minutes=5),
            created_at=T0 + timedelta(seconds=1),
        )

        repo = JobRepository(factory)
        candidates = repo.discover_due_candidates(role=JobRole.CORE, now=T0, limit=10)
        ids = [c.job_id for c in candidates]
        assert "future" not in ids
        assert "agent-only" not in ids
        assert ids == ["high-pri-early", "high-pri-late", "low-pri"]

        agent = repo.discover_due_candidates(role=JobRole.AGENT, now=T0, limit=10)
        assert [c.job_id for c in agent] == ["agent-only"]
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Single-claim and dual-client CAS
# ---------------------------------------------------------------------------


def test_exactly_one_winner_same_expected_fence(concurrency_db: str) -> None:
    factory_a, factory_b, engine_a, engine_b = _client_pair(concurrency_db)
    try:
        _seed_tenant(factory_a)
        _add_job(factory_a, job_id="job-race", fence_token=3, priority=1)

        # Both clients discover the same expected fence independently.
        repo_a = JobRepository(factory_a)
        repo_b = JobRepository(factory_b)
        candidates_a = repo_a.discover_due_candidates(role=JobRole.CORE, now=T0, limit=1)
        candidates_b = repo_b.discover_due_candidates(role=JobRole.CORE, now=T0, limit=1)
        assert len(candidates_a) == 1
        assert len(candidates_b) == 1
        assert candidates_a[0].expected_fence_token == 3
        assert candidates_b[0].expected_fence_token == 3

        barrier = threading.Barrier(2)
        results: list[object] = [None, None]
        errors: list[BaseException] = []

        def worker(idx: int, repo: JobRepository, candidate: JobCandidate) -> None:
            try:
                barrier.wait(timeout=5)
                results[idx] = repo.try_claim_job(
                    candidate,
                    owner=f"worker-{idx}",
                    lease_ttl=LEASE_TTL,
                    now=T0,
                )
            except BaseException as exc:
                errors.append(exc)

        t0 = threading.Thread(target=worker, args=(0, repo_a, candidates_a[0]), name="claim-a")
        t1 = threading.Thread(target=worker, args=(1, repo_b, candidates_b[0]), name="claim-b")
        t0.start()
        t1.start()
        t0.join(timeout=10)
        t1.join(timeout=10)
        assert not t0.is_alive()
        assert not t1.is_alive()
        assert not errors
        wins = [r for r in results if r is not None]
        losses = [r for r in results if r is None]
        assert len(wins) == 1
        assert len(losses) == 1
        claim = wins[0]
        assert isinstance(claim, JobClaim)
        assert claim.fence_token == 4  # incremented from 3
        assert claim.attempts == 1
        assert claim.owner in {"worker-0", "worker-1"}

        with factory_a() as session:
            job = session.get(Job, "job-race")
            assert job is not None
            assert job.status == JobStatus.LEASED.value
            assert job.fence_token == 4
            lease = session.get(JobLease, "job-race")
            assert lease is not None
            assert lease.fence_token == 4
            assert lease.lease_owner == claim.owner
    finally:
        engine_a.dispose()
        engine_b.dispose()


def test_claim_next_loser_retries_next_candidate(concurrency_db: str) -> None:
    factory_a, factory_b, engine_a, engine_b = _client_pair(concurrency_db)
    try:
        _seed_tenant(factory_a)
        _add_job(
            factory_a,
            job_id="first",
            priority=1,
            run_after=T0 - timedelta(minutes=1),
            created_at=T0,
        )
        _add_job(
            factory_a,
            job_id="second",
            priority=2,
            run_after=T0 - timedelta(minutes=1),
            created_at=T0,
        )

        repo_a = JobRepository(factory_a)
        repo_b = JobRepository(factory_b)
        # A wins the first candidate.
        claim_a = repo_a.claim_next(
            role=JobRole.CORE,
            owner="a",
            lease_ttl=LEASE_TTL,
            now=T0,
            limit=8,
        )
        assert claim_a is not None
        assert claim_a.job_id == "first"

        # B's claim_next must lose on first and win second (retry).
        claim_b = repo_b.claim_next(
            role=JobRole.CORE,
            owner="b",
            lease_ttl=LEASE_TTL,
            now=T0,
            limit=8,
        )
        assert claim_b is not None
        assert claim_b.job_id == "second"
        assert claim_b.owner == "b"
    finally:
        engine_a.dispose()
        engine_b.dispose()


def test_concurrent_workers_distribute_many_jobs(concurrency_db: str) -> None:
    """Many jobs, concurrent workers: no duplicate claims, no silent loss.

    Bounded stress shape: fixed job count, two independent client engines,
    barrier start. libSQL write serialization is real; workers rely solely on
    JobRepository bounded lock-contention handling (no test-level lock catch).
    Fairness (both workers win) is not a CAS guarantee and is not asserted.
    """
    n_jobs = 24
    n_workers = 2
    settings = Settings(database_url=concurrency_db)
    seed_engine = create_db_engine(settings)
    seed_factory = create_session_factory(seed_engine)
    try:
        _seed_tenant(seed_factory)
        for i in range(n_jobs):
            _add_job(
                seed_factory,
                job_id=f"job-{i:03d}",
                priority=i % 5,
                run_after=T0 - timedelta(seconds=i),
                created_at=T0 + timedelta(seconds=i),
            )
    finally:
        seed_engine.dispose()

    barrier = threading.Barrier(n_workers)
    claimed_ids: list[str] = []
    lock = threading.Lock()
    errors: list[BaseException] = []

    def worker_loop(worker_id: int) -> None:
        engine = create_db_engine(settings)
        factory = create_session_factory(engine)
        repo = JobRepository(factory)
        try:
            barrier.wait(timeout=5)
            empty_streak = 0
            # Drain until repeated empty discovers (other worker may finish first).
            while empty_streak < 3:
                claim = repo.claim_next(
                    role=JobRole.CORE,
                    owner=f"w{worker_id}",
                    lease_ttl=LEASE_TTL,
                    now=T0,
                    limit=16,
                )
                if claim is None:
                    empty_streak += 1
                    continue
                empty_streak = 0
                with lock:
                    claimed_ids.append(claim.job_id)
        except BaseException as exc:
            errors.append(exc)
        finally:
            engine.dispose()

    threads = [
        threading.Thread(target=worker_loop, args=(i,), name=f"worker-{i}")
        for i in range(n_workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), f"thread {t.name} still alive after join"
    assert not errors, f"worker errors: {errors}"
    assert len(claimed_ids) == n_jobs
    assert len(set(claimed_ids)) == n_jobs

    # Every job leased exactly once in DB.
    verify_engine = create_db_engine(settings)
    try:
        with create_session_factory(verify_engine)() as session:
            rows = session.scalars(select(Job)).all()
            assert len(rows) == n_jobs
            assert all(j.status == JobStatus.LEASED.value for j in rows)
            leases = session.scalars(select(JobLease)).all()
            assert len(leases) == n_jobs
            owners = {lease.lease_owner for lease in leases}
            assert owners <= {f"w{i}" for i in range(n_workers)}
    finally:
        verify_engine.dispose()


# ---------------------------------------------------------------------------
# Heartbeat and completion
# ---------------------------------------------------------------------------


def test_heartbeat_success_and_stale_reject(concurrency_db: str) -> None:
    factory, _, engine, _ = _client_pair(concurrency_db)
    try:
        _seed_tenant(factory)
        _add_job(factory, job_id="hb-job", priority=1)
        _add_job(factory, job_id="hb-expire", priority=2)
        repo = JobRepository(factory)
        claim = repo.claim_next(role=JobRole.CORE, owner="owner-a", lease_ttl=LEASE_TTL, now=T0)
        assert claim is not None
        assert claim.job_id == "hb-job"

        ok = repo.heartbeat_job(
            job_id=claim.job_id,
            owner=claim.owner,
            fence_token=claim.fence_token,
            lease_ttl=LEASE_TTL,
            now=T0 + timedelta(seconds=5),
        )
        assert ok is True

        # Stale fence
        assert (
            repo.heartbeat_job(
                job_id=claim.job_id,
                owner=claim.owner,
                fence_token=claim.fence_token - 1,
                lease_ttl=LEASE_TTL,
                now=T0 + timedelta(seconds=6),
            )
            is False
        )
        # Wrong owner
        assert (
            repo.heartbeat_job(
                job_id=claim.job_id,
                owner="other",
                fence_token=claim.fence_token,
                lease_ttl=LEASE_TTL,
                now=T0 + timedelta(seconds=6),
            )
            is False
        )

        # Expired lease: use a separate claim that was never heartbeated.
        claim_exp = repo.claim_next(role=JobRole.CORE, owner="owner-a", lease_ttl=LEASE_TTL, now=T0)
        assert claim_exp is not None
        assert claim_exp.job_id == "hb-expire"
        past_expiry = T0 + LEASE_TTL + timedelta(seconds=1)
        assert (
            repo.heartbeat_job(
                job_id=claim_exp.job_id,
                owner=claim_exp.owner,
                fence_token=claim_exp.fence_token,
                lease_ttl=LEASE_TTL,
                now=past_expiry,
            )
            is False
        )
    finally:
        engine.dispose()


def test_heartbeat_shorter_horizon_preserves_expiry(concurrency_db: str) -> None:
    """Heartbeat with shorter lease_ttl never shortens existing leased_until."""
    factory, _, engine, _ = _client_pair(concurrency_db)
    try:
        _seed_tenant(factory)
        _add_job(factory, job_id="hz-job", priority=1)
        repo = JobRepository(factory)
        long_ttl = timedelta(seconds=60)
        short_ttl = timedelta(seconds=10)
        claim = repo.claim_next(role=JobRole.CORE, owner="owner-a", lease_ttl=long_ttl, now=T0)
        assert claim is not None
        original_leased_until = claim.leased_until

        ok = repo.heartbeat_job(
            job_id=claim.job_id,
            owner=claim.owner,
            fence_token=claim.fence_token,
            lease_ttl=short_ttl,
            now=T0 + timedelta(seconds=5),
        )
        assert ok is True
        with factory() as session:
            lease = session.get(JobLease, claim.job_id)
            assert lease is not None
            assert lease.leased_until == original_leased_until
    finally:
        engine.dispose()


def test_heartbeat_later_horizon_extends_job(concurrency_db: str) -> None:
    """Heartbeat with longer lease_ttl extends leased_until to new deadline."""
    factory, _, engine, _ = _client_pair(concurrency_db)
    try:
        _seed_tenant(factory)
        _add_job(factory, job_id="hz-extend", priority=1)
        repo = JobRepository(factory)
        short_ttl = timedelta(seconds=10)
        long_ttl = timedelta(seconds=60)
        claim = repo.claim_next(role=JobRole.CORE, owner="owner-a", lease_ttl=short_ttl, now=T0)
        assert claim is not None
        assert claim.leased_until == to_utc_rfc3339(T0 + short_ttl)

        ok = repo.heartbeat_job(
            job_id=claim.job_id,
            owner=claim.owner,
            fence_token=claim.fence_token,
            lease_ttl=long_ttl,
            now=T0 + timedelta(seconds=5),
        )
        assert ok is True
        with factory() as session:
            lease = session.get(JobLease, claim.job_id)
            assert lease is not None
            assert lease.leased_until == to_utc_rfc3339(T0 + timedelta(seconds=5) + long_ttl)
    finally:
        engine.dispose()


def test_heartbeat_leader_horizon_never_shortens(concurrency_db: str) -> None:
    """Leader heartbeat with shorter lease_ttl preserves existing leased_until."""
    factory, _, engine, _ = _client_pair(concurrency_db)
    try:
        repo = JobRepository(factory)
        long_ttl = timedelta(seconds=60)
        short_ttl = timedelta(seconds=10)
        leader = repo.try_acquire_leader(
            lease_name="core-worker",
            owner="leader-a",
            lease_ttl=long_ttl,
            now=T0,
        )
        assert leader is not None
        original_leased_until = leader.leased_until

        ok = repo.heartbeat_leader(
            lease_name="core-worker",
            owner="leader-a",
            fence_token=leader.fence_token,
            lease_ttl=short_ttl,
            now=T0 + timedelta(seconds=5),
        )
        assert ok is True
        with factory() as session:
            row = session.get(LeaderLease, "core-worker")
            assert row is not None
            assert row.leased_until == original_leased_until
    finally:
        engine.dispose()


def test_heartbeat_leader_later_horizon_extends(concurrency_db: str) -> None:
    """Leader heartbeat with longer lease_ttl extends leased_until to new deadline."""
    factory, _, engine, _ = _client_pair(concurrency_db)
    try:
        repo = JobRepository(factory)
        short_ttl = timedelta(seconds=10)
        long_ttl = timedelta(seconds=60)
        leader = repo.try_acquire_leader(
            lease_name="core-worker",
            owner="leader-a",
            lease_ttl=short_ttl,
            now=T0,
        )
        assert leader is not None
        assert leader.leased_until == to_utc_rfc3339(T0 + short_ttl)

        ok = repo.heartbeat_leader(
            lease_name="core-worker",
            owner="leader-a",
            fence_token=leader.fence_token,
            lease_ttl=long_ttl,
            now=T0 + timedelta(seconds=5),
        )
        assert ok is True
        with factory() as session:
            row = session.get(LeaderLease, "core-worker")
            assert row is not None
            assert row.leased_until == to_utc_rfc3339(T0 + timedelta(seconds=5) + long_ttl)
    finally:
        engine.dispose()


def test_complete_success_and_rejects(concurrency_db: str) -> None:
    factory, _, engine, _ = _client_pair(concurrency_db)
    try:
        _seed_tenant(factory)
        _add_job(factory, job_id="c1")
        _add_job(factory, job_id="c2")
        _add_job(factory, job_id="c3")
        repo = JobRepository(factory)

        claim1 = repo.claim_next(role=JobRole.CORE, owner="owner-a", lease_ttl=LEASE_TTL, now=T0)
        assert claim1 is not None
        assert (
            repo.complete_job(
                job_id=claim1.job_id,
                owner=claim1.owner,
                fence_token=claim1.fence_token,
                now=T0 + timedelta(seconds=1),
            )
            is True
        )
        with factory() as session:
            job = session.get(Job, claim1.job_id)
            assert job is not None
            assert job.status == JobStatus.SUCCEEDED.value
            assert session.get(JobLease, claim1.job_id) is None

        claim2 = repo.claim_next(role=JobRole.CORE, owner="owner-a", lease_ttl=LEASE_TTL, now=T0)
        assert claim2 is not None
        # Wrong owner
        assert (
            repo.complete_job(
                job_id=claim2.job_id,
                owner="wrong",
                fence_token=claim2.fence_token,
                now=T0 + timedelta(seconds=1),
            )
            is False
        )
        # Stale fence
        assert (
            repo.complete_job(
                job_id=claim2.job_id,
                owner=claim2.owner,
                fence_token=claim2.fence_token + 99,
                now=T0 + timedelta(seconds=1),
            )
            is False
        )

        claim3 = repo.claim_next(role=JobRole.CORE, owner="owner-a", lease_ttl=LEASE_TTL, now=T0)
        assert claim3 is not None
        # Expired
        assert (
            repo.complete_job(
                job_id=claim3.job_id,
                owner=claim3.owner,
                fence_token=claim3.fence_token,
                now=T0 + LEASE_TTL + timedelta(seconds=1),
            )
            is False
        )
    finally:
        engine.dispose()


def test_has_valid_job_lease_current_and_stale(concurrency_db: str) -> None:
    """has_valid_job_lease is a validity primitive, not side-effect fencing."""
    factory, _, engine, _ = _client_pair(concurrency_db)
    try:
        _seed_tenant(factory)
        _add_job(factory, job_id="valid-lease-job")
        repo = JobRepository(factory)
        claim = repo.claim_next(role=JobRole.CORE, owner="owner-a", lease_ttl=LEASE_TTL, now=T0)
        assert claim is not None

        assert (
            repo.has_valid_job_lease(
                job_id=claim.job_id,
                owner=claim.owner,
                fence_token=claim.fence_token,
                now=T0 + timedelta(seconds=1),
            )
            is True
        )
        # Stale fence
        assert (
            repo.has_valid_job_lease(
                job_id=claim.job_id,
                owner=claim.owner,
                fence_token=claim.fence_token - 1,
                now=T0 + timedelta(seconds=1),
            )
            is False
        )
        # Wrong owner
        assert (
            repo.has_valid_job_lease(
                job_id=claim.job_id,
                owner="other",
                fence_token=claim.fence_token,
                now=T0 + timedelta(seconds=1),
            )
            is False
        )
        # Expired
        assert (
            repo.has_valid_job_lease(
                job_id=claim.job_id,
                owner=claim.owner,
                fence_token=claim.fence_token,
                now=T0 + LEASE_TTL + timedelta(seconds=1),
            )
            is False
        )
        # After complete: job no longer leased
        assert (
            repo.complete_job(
                job_id=claim.job_id,
                owner=claim.owner,
                fence_token=claim.fence_token,
                now=T0 + timedelta(seconds=2),
            )
            is True
        )
        assert (
            repo.has_valid_job_lease(
                job_id=claim.job_id,
                owner=claim.owner,
                fence_token=claim.fence_token,
                now=T0 + timedelta(seconds=3),
            )
            is False
        )
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Requeue and dead-letter
# ---------------------------------------------------------------------------


def test_requeue_expired_increments_fence_invalidates_stale(
    concurrency_db: str,
) -> None:
    factory, _, engine, _ = _client_pair(concurrency_db)
    try:
        _seed_tenant(factory)
        _add_job(factory, job_id="rq-job", max_attempts=5)
        repo = JobRepository(factory)
        claim = repo.claim_next(role=JobRole.CORE, owner="old-owner", lease_ttl=LEASE_TTL, now=T0)
        assert claim is not None
        old_fence = claim.fence_token

        expired_now = T0 + LEASE_TTL + timedelta(seconds=1)
        n = repo.requeue_expired_leases(now=expired_now)
        assert n == 1

        with factory() as session:
            job = session.get(Job, "rq-job")
            assert job is not None
            assert job.status == JobStatus.READY.value
            assert job.fence_token == old_fence + 1
            assert session.get(JobLease, "rq-job") is None

        # Stale owner cannot heartbeat or complete with old fence.
        assert (
            repo.heartbeat_job(
                job_id="rq-job",
                owner="old-owner",
                fence_token=old_fence,
                lease_ttl=LEASE_TTL,
                now=expired_now,
            )
            is False
        )
        assert (
            repo.complete_job(
                job_id="rq-job",
                owner="old-owner",
                fence_token=old_fence,
                now=expired_now,
            )
            is False
        )

        # Fresh claim works with new expected fence.
        reclaim = repo.claim_next(
            role=JobRole.CORE,
            owner="new-owner",
            lease_ttl=LEASE_TTL,
            now=expired_now,
        )
        assert reclaim is not None
        assert reclaim.owner == "new-owner"
        assert reclaim.fence_token == old_fence + 2
    finally:
        engine.dispose()


def test_concurrent_reapers_exactly_one_win(concurrency_db: str) -> None:
    """Two clients reaping the same expired lease: combined wins == 1, fence +1."""
    factory_a, factory_b, engine_a, engine_b = _client_pair(concurrency_db)
    try:
        _seed_tenant(factory_a)
        _add_job(factory_a, job_id="reap-race", max_attempts=5)
        repo_a = JobRepository(factory_a)
        claim = repo_a.claim_next(
            role=JobRole.CORE,
            owner="worker",
            lease_ttl=LEASE_TTL,
            now=T0,
        )
        assert claim is not None
        pre_fence = claim.fence_token
        expired_now = T0 + LEASE_TTL + timedelta(seconds=1)

        repo_b = JobRepository(factory_b)
        barrier = threading.Barrier(2)
        results: list[int | None] = [None, None]
        errors: list[BaseException] = []

        def reaper(idx: int, repo: JobRepository) -> None:
            try:
                barrier.wait(timeout=5)
                results[idx] = repo.requeue_expired_leases(now=expired_now)
            except BaseException as exc:
                errors.append(exc)

        t0 = threading.Thread(target=reaper, args=(0, repo_a), name="reaper-a")
        t1 = threading.Thread(target=reaper, args=(1, repo_b), name="reaper-b")
        t0.start()
        t1.start()
        t0.join(timeout=10)
        t1.join(timeout=10)
        assert not t0.is_alive()
        assert not t1.is_alive()
        assert not errors, f"reaper errors: {errors}"
        assert results[0] is not None and results[1] is not None
        assert results[0] + results[1] == 1
        assert {results[0], results[1]} == {0, 1}

        with factory_a() as session:
            job = session.get(Job, "reap-race")
            assert job is not None
            assert job.status == JobStatus.READY.value
            assert job.fence_token == pre_fence + 1
            assert session.get(JobLease, "reap-race") is None
    finally:
        engine_a.dispose()
        engine_b.dispose()


def test_dead_letter_expired_at_max_attempts(concurrency_db: str) -> None:
    factory, _, engine, _ = _client_pair(concurrency_db)
    try:
        _seed_tenant(factory)
        # attempts will become 1 on claim; max_attempts=1 → dead letter on expiry.
        _add_job(factory, job_id="dl-job", attempts=0, max_attempts=1)
        repo = JobRepository(factory)
        claim = repo.claim_next(role=JobRole.CORE, owner="owner", lease_ttl=LEASE_TTL, now=T0)
        assert claim is not None
        assert claim.attempts == 1

        expired_now = T0 + LEASE_TTL + timedelta(seconds=1)
        assert repo.requeue_expired_leases(now=expired_now) == 0
        n = repo.dead_letter_expired_jobs(now=expired_now)
        assert n == 1

        with factory() as session:
            job = session.get(Job, "dl-job")
            assert job is not None
            assert job.status == JobStatus.DEAD_LETTER.value
            assert session.get(JobLease, "dl-job") is None
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Leader fencing
# ---------------------------------------------------------------------------


def test_two_leader_contenders_one_winner(concurrency_db: str) -> None:
    factory_a, factory_b, engine_a, engine_b = _client_pair(concurrency_db)
    try:
        repo_a = JobRepository(factory_a)
        repo_b = JobRepository(factory_b)
        # Pre-create the coordination row so the race is pure CAS update, not insert.
        assert (
            repo_a.try_acquire_leader(
                lease_name="core-worker",
                owner="bootstrap",
                lease_ttl=timedelta(seconds=1),
                now=T0 - timedelta(hours=1),
            )
            is not None
        )
        # Force expiry so both contenders race on a free expired lease.
        expired_now = T0

        barrier = threading.Barrier(2)
        results: list[object] = [None, None]
        errors: list[BaseException] = []

        def contender(idx: int, repo: JobRepository) -> None:
            try:
                barrier.wait(timeout=5)
                results[idx] = repo.try_acquire_leader(
                    lease_name="core-worker",
                    owner=f"leader-{idx}",
                    lease_ttl=LEASE_TTL,
                    now=expired_now,
                )
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=contender, args=(0, repo_a), name="leader-0"),
            threading.Thread(target=contender, args=(1, repo_b), name="leader-1"),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
            assert not t.is_alive(), f"thread {t.name} still alive after join"
        assert not errors, f"leader race errors: {errors}"
        wins = [r for r in results if r is not None]
        losses = [r for r in results if r is None]
        assert len(wins) == 1
        assert len(losses) == 1
        winner = wins[0]
        assert isinstance(winner, LeaderClaim)
        assert winner.fence_token >= 2  # bootstrap was 1; takeover increments
        assert winner.owner in {"leader-0", "leader-1"}
        assert repo_a.has_valid_leadership(
            lease_name="core-worker",
            owner=winner.owner,
            fence_token=winner.fence_token,
            now=expired_now + timedelta(seconds=1),
        )
        loser_owner = "leader-0" if winner.owner == "leader-1" else "leader-1"
        assert not repo_a.has_valid_leadership(
            lease_name="core-worker",
            owner=loser_owner,
            fence_token=winner.fence_token,
            now=expired_now + timedelta(seconds=1),
        )
        # Bootstrap former owner cannot heartbeat with stale fence.
        assert (
            repo_a.heartbeat_leader(
                lease_name="core-worker",
                owner="bootstrap",
                fence_token=1,
                lease_ttl=LEASE_TTL,
                now=expired_now + timedelta(seconds=1),
            )
            is False
        )
    finally:
        engine_a.dispose()
        engine_b.dispose()


def test_expired_leader_takeover_increments_fence(concurrency_db: str) -> None:
    factory, _, engine, _ = _client_pair(concurrency_db)
    try:
        repo = JobRepository(factory)
        first = repo.try_acquire_leader(
            lease_name="core-worker",
            owner="former",
            lease_ttl=LEASE_TTL,
            now=T0,
        )
        assert first is not None
        assert first.fence_token == 1

        # Heartbeat succeeds while unexpired.
        assert (
            repo.heartbeat_leader(
                lease_name="core-worker",
                owner="former",
                fence_token=first.fence_token,
                lease_ttl=LEASE_TTL,
                now=T0 + timedelta(seconds=5),
            )
            is True
        )

        expired_now = T0 + LEASE_TTL + timedelta(seconds=10)
        second = repo.try_acquire_leader(
            lease_name="core-worker",
            owner="promoted",
            lease_ttl=LEASE_TTL,
            now=expired_now,
        )
        assert second is not None
        assert second.owner == "promoted"
        assert second.fence_token == first.fence_token + 1

        # Stale former leader cannot heartbeat.
        assert (
            repo.heartbeat_leader(
                lease_name="core-worker",
                owner="former",
                fence_token=first.fence_token,
                lease_ttl=LEASE_TTL,
                now=expired_now + timedelta(seconds=1),
            )
            is False
        )
        assert not repo.has_valid_leadership(
            lease_name="core-worker",
            owner="former",
            fence_token=first.fence_token,
            now=expired_now + timedelta(seconds=1),
        )
        assert repo.has_valid_leadership(
            lease_name="core-worker",
            owner="promoted",
            fence_token=second.fence_token,
            now=expired_now + timedelta(seconds=1),
        )
    finally:
        engine.dispose()


def test_leader_lease_constraints_owner_expiry_pair(concurrency_db: str) -> None:
    """owner/expiry must both be null or both non-null; name nonempty in model."""
    factory, _, engine, _ = _client_pair(concurrency_db)
    try:
        # Free row: both null OK.
        with factory() as session, session.begin():
            session.add(
                LeaderLease(
                    lease_name="ok-free",
                    lease_owner=None,
                    leased_until=None,
                    fence_token=0,
                    updated_at=to_utc_rfc3339(T0),
                )
            )
        # Held row: both non-null OK.
        with factory() as session, session.begin():
            session.add(
                LeaderLease(
                    lease_name="ok-held",
                    lease_owner="owner",
                    leased_until=to_utc_rfc3339(T0 + LEASE_TTL),
                    fence_token=1,
                    updated_at=to_utc_rfc3339(T0),
                )
            )
        # Owner without expiry: rejected.
        with factory() as session, session.begin():
            session.add(
                LeaderLease(
                    lease_name="bad-owner-only",
                    lease_owner="owner",
                    leased_until=None,
                    fence_token=0,
                    updated_at=to_utc_rfc3339(T0),
                )
            )
            with pytest.raises(ConstraintError):
                session.flush()
        # Expiry without owner: rejected.
        with factory() as session, session.begin():
            session.add(
                LeaderLease(
                    lease_name="bad-expiry-only",
                    lease_owner=None,
                    leased_until=to_utc_rfc3339(T0 + LEASE_TTL),
                    fence_token=0,
                    updated_at=to_utc_rfc3339(T0),
                )
            )
            with pytest.raises(ConstraintError):
                session.flush()
        # Empty owner when non-null: rejected.
        with factory() as session, session.begin():
            session.add(
                LeaderLease(
                    lease_name="bad-empty-owner",
                    lease_owner="",
                    leased_until=to_utc_rfc3339(T0 + LEASE_TTL),
                    fence_token=0,
                    updated_at=to_utc_rfc3339(T0),
                )
            )
            with pytest.raises(ConstraintError):
                session.flush()
    finally:
        engine.dispose()


def test_leader_model_check_constraints_agree_with_migration() -> None:
    """ORM declares nonempty name + owner/expiry pair checks for leader_leases."""
    leader_table = LeaderLease.__table__
    assert isinstance(leader_table, Table)
    check_constraints = [ck for ck in leader_table.constraints if isinstance(ck, CheckConstraint)]
    sql_texts = [str(ck.sqltext) for ck in check_constraints]
    assert any("length(lease_name)" in sql for sql in sql_texts)
    assert any("lease_owner IS NULL AND leased_until IS NULL" in sql for sql in sql_texts)
    assert any("lease_owner IS NOT NULL AND leased_until IS NOT NULL" in sql for sql in sql_texts)


# ---------------------------------------------------------------------------
# Nested transactions / savepoints
# ---------------------------------------------------------------------------


def test_nested_transaction_savepoint_behavior(concurrency_db: str) -> None:
    factory, _, engine, _ = _client_pair(concurrency_db)
    try:
        _seed_tenant(factory)
        with factory() as session, session.begin():
            session.add(
                Job(
                    id="outer-job",
                    tenant_id="tenant-1",
                    role="core",
                    status="ready",
                    payload_json='{"kind":"outer"}',
                    priority=1,
                    run_after=to_utc_rfc3339(T0),
                    attempts=0,
                    max_attempts=5,
                    idempotency_key="outer-job",
                    fence_token=0,
                    created_at=to_utc_rfc3339(T0),
                    updated_at=to_utc_rfc3339(T0),
                    job_type="system.noop",
                )
            )
            session.flush()
            # Nested savepoint: insert then roll back nested only.
            nested = session.begin_nested()
            session.add(
                Job(
                    id="nested-job",
                    tenant_id="tenant-1",
                    role="core",
                    status="ready",
                    payload_json='{"kind":"nested"}',
                    priority=1,
                    run_after=to_utc_rfc3339(T0),
                    attempts=0,
                    max_attempts=5,
                    idempotency_key="nested-job",
                    fence_token=0,
                    created_at=to_utc_rfc3339(T0),
                    updated_at=to_utc_rfc3339(T0),
                    job_type="system.noop",
                )
            )
            session.flush()
            nested.rollback()

            assert session.get(Job, "outer-job") is not None
            assert session.get(Job, "nested-job") is None

        with factory() as session:
            assert session.get(Job, "outer-job") is not None
            assert session.get(Job, "nested-job") is None
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Migration cycle and model agreement for lease tables
# ---------------------------------------------------------------------------


def test_migration_upgrade_downgrade_to_0001_upgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "mig.db"
    url = f"sqlite+libsql:///{db_path.resolve()}"
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", url)
    clear_settings_cache()
    cfg = _alembic_config(url)

    command.upgrade(cfg, "head")
    engine = create_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert "job_leases" in tables
        assert "leader_leases" in tables
        assert "job_attempts" in tables
        assert "job_dead_letters" in tables
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert version == head_revision()
    finally:
        engine.dispose()

    command.downgrade(cfg, "20260713_0001")
    engine = create_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert "job_leases" not in tables
        assert "leader_leases" not in tables
        assert "job_attempts" not in tables
        assert "job_dead_letters" not in tables
        assert "jobs" in tables
        assert "tenants" in tables
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert version == "20260713_0001"
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")
    engine = create_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert "job_leases" in tables
        assert "leader_leases" in tables
        assert "job_attempts" in tables
        assert "job_dead_letters" in tables
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert version == head_revision()
        assert db_path.resolve().is_relative_to(tmp_path.resolve())
    finally:
        engine.dispose()
        clear_settings_cache()


def test_lease_models_agree_with_migration(concurrency_db: str) -> None:
    settings = Settings(database_url=concurrency_db)
    engine = create_db_engine(settings)
    try:
        insp = inspect(engine)
        assert set(insp.get_table_names()) >= {
            "tenants",
            "jobs",
            "job_leases",
            "leader_leases",
            "job_attempts",
            "job_dead_letters",
            "alembic_version",
        }
        lease_cols = {c["name"] for c in insp.get_columns("job_leases")}
        leader_cols = {c["name"] for c in insp.get_columns("leader_leases")}
        attempt_cols = {c["name"] for c in insp.get_columns("job_attempts")}
        dl_cols = {c["name"] for c in insp.get_columns("job_dead_letters")}
        assert lease_cols == {c.name for c in JobLease.__table__.columns}
        assert leader_cols == {c.name for c in LeaderLease.__table__.columns}
        assert attempt_cols == {c.name for c in JobAttempt.__table__.columns}
        assert dl_cols == {c.name for c in JobDeadLetter.__table__.columns}

        fks = insp.get_foreign_keys("job_leases")
        assert any(
            fk["referred_table"] == "jobs" and fk["constrained_columns"] == ["job_id"] for fk in fks
        )
        lease_indexes = {ix["name"] for ix in insp.get_indexes("job_leases")}
        assert "ix_job_leases_leased_until" in lease_indexes
        assert "ix_job_leases_lease_owner" in lease_indexes
        leader_indexes = {ix["name"] for ix in insp.get_indexes("leader_leases")}
        assert "ix_leader_leases_leased_until" in leader_indexes

        attempt_fks = insp.get_foreign_keys("job_attempts")
        assert any(
            fk["referred_table"] == "jobs" and fk["constrained_columns"] == ["job_id"]
            for fk in attempt_fks
        )
        attempt_indexes = {ix["name"] for ix in insp.get_indexes("job_attempts")}
        assert "ix_job_attempts_job_id" in attempt_indexes
        assert "ix_job_attempts_status" in attempt_indexes

        dl_fks = insp.get_foreign_keys("job_dead_letters")
        assert any(
            fk["referred_table"] == "jobs" and fk["constrained_columns"] == ["job_id"]
            for fk in dl_fks
        )
        assert any(
            fk["referred_table"] == "tenants" and fk["constrained_columns"] == ["tenant_id"]
            for fk in dl_fks
        )
        dl_indexes = {ix["name"] for ix in insp.get_indexes("job_dead_letters")}
        assert "ix_job_dead_letters_tenant_dead_lettered_at" in dl_indexes
    finally:
        engine.dispose()


def test_repository_source_has_no_for_update_or_skip_locked() -> None:
    source_path = _backend_root() / "src" / "akunaki" / "adapters" / "db" / "job_repository.py"
    text_src = source_path.read_text(encoding="utf-8")
    upper = text_src.upper()
    # Literal SQL lock idioms must not appear in the repository implementation.
    assert "FOR UPDATE" not in upper
    assert "SKIP LOCKED" not in upper
    assert "WITH_FOR_UPDATE" not in upper


def test_busy_timeout_pragma_set(concurrency_db: str) -> None:
    settings = Settings(database_url=concurrency_db)
    engine = create_db_engine(settings)
    try:
        with engine.connect() as conn:
            value = conn.execute(text("PRAGMA busy_timeout")).scalar_one()
        assert int(value) == BUSY_TIMEOUT_MS
        assert BUSY_TIMEOUT_MS == 50
    finally:
        engine.dispose()


def test_file_wal_enabled_memory_does_not_require_wal(tmp_path: Path) -> None:
    """WAL is set once for file-backed engines; in-memory engines never set WAL."""
    file_url = f"sqlite+libsql:///{(tmp_path / 'wal.db').resolve()}"
    file_engine = create_db_engine(Settings(database_url=file_url))
    try:
        with file_engine.connect() as conn:
            mode = conn.execute(text("PRAGMA journal_mode")).scalar_one()
            fk = conn.execute(text("PRAGMA foreign_keys")).scalar_one()
        assert str(mode).lower() == "wal"
        assert int(fk) == 1
    finally:
        file_engine.dispose()

    mem_engine = create_db_engine(Settings(database_url="sqlite+libsql:///:memory:"))
    try:
        with mem_engine.connect() as conn:
            fk = conn.execute(text("PRAGMA foreign_keys")).scalar_one()
            busy = conn.execute(text("PRAGMA busy_timeout")).scalar_one()
        assert int(fk) == 1
        assert int(busy) == BUSY_TIMEOUT_MS
        # Engine is usable without WAL configuration for in-memory.
        assert probe_database_ready(mem_engine)
    finally:
        mem_engine.dispose()


def test_try_claim_rejects_naive_now(concurrency_db: str) -> None:
    factory, _, engine, _ = _client_pair(concurrency_db)
    try:
        _seed_tenant(factory)
        _add_job(factory, job_id="naive-job")
        repo = JobRepository(factory)
        candidates = repo.discover_due_candidates(
            role=JobRole.CORE,
            now=T0,
            limit=1,
        )
        assert len(candidates) == 1
        with pytest.raises(ValueError, match="timezone-aware"):
            repo.try_claim_job(
                candidates[0],
                owner="x",
                lease_ttl=LEASE_TTL,
                now=datetime(2026, 7, 13, 12, 0, 0),  # naive
            )
    finally:
        engine.dispose()


def test_claim_next_validates_before_discovery_empty_queue(concurrency_db: str) -> None:
    """Owner, TTL, and limit are validated even when the queue is empty."""
    factory, _, engine, _ = _client_pair(concurrency_db)
    try:
        _seed_tenant(factory)
        repo = JobRepository(factory)
        with pytest.raises(ValueError, match="owner must be non-empty"):
            repo.claim_next(
                role=JobRole.CORE,
                owner="",
                lease_ttl=LEASE_TTL,
                now=T0,
            )
        with pytest.raises(ValueError, match="at least one second"):
            repo.claim_next(
                role=JobRole.CORE,
                owner="owner",
                lease_ttl=timedelta(milliseconds=500),
                now=T0,
            )
        with pytest.raises(ValueError, match="limit must be >= 1"):
            repo.claim_next(
                role=JobRole.CORE,
                owner="owner",
                lease_ttl=LEASE_TTL,
                now=T0,
                limit=0,
            )
        # Empty queue still returns None after valid args.
        assert (
            repo.claim_next(
                role=JobRole.CORE,
                owner="owner",
                lease_ttl=LEASE_TTL,
                now=T0,
            )
            is None
        )
    finally:
        engine.dispose()


def test_lease_ttl_rejects_subsecond_and_zero(concurrency_db: str) -> None:
    factory, _, engine, _ = _client_pair(concurrency_db)
    try:
        _seed_tenant(factory)
        _add_job(factory, job_id="ttl-job")
        repo = JobRepository(factory)
        candidates = repo.discover_due_candidates(role=JobRole.CORE, now=T0, limit=1)
        assert len(candidates) == 1
        with pytest.raises(ValueError, match="at least one second"):
            repo.try_claim_job(
                candidates[0],
                owner="owner",
                lease_ttl=timedelta(milliseconds=1),
                now=T0,
            )
        with pytest.raises(ValueError, match="at least one second"):
            repo.try_claim_job(
                candidates[0],
                owner="owner",
                lease_ttl=timedelta(0),
                now=T0,
            )
        with pytest.raises(ValueError, match="at least one second"):
            repo.try_acquire_leader(
                lease_name="core-worker",
                owner="owner",
                lease_ttl=timedelta(microseconds=500),
                now=T0,
            )
        # Exactly one second is accepted.
        claim = repo.try_claim_job(
            candidates[0],
            owner="owner",
            lease_ttl=MIN_LEASE_TTL,
            now=T0,
        )
        assert claim is not None
        assert claim.leased_until == to_utc_rfc3339(T0 + MIN_LEASE_TTL)
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# QueuePool connection reuse
# ---------------------------------------------------------------------------


def test_queuepool_sequential_checkouts_reuse_connection(concurrency_db: str) -> None:
    """Sequential QueuePool checkouts reuse the same physical DB-API connection.

    Uses a SQLAlchemy connect event counter to prove that repeated Session
    checkouts on the same engine do not open new physical connections.
    """
    settings = Settings(database_url=concurrency_db)
    engine = create_db_engine(settings)
    try:
        connect_count = 0

        @event.listens_for(engine, "connect")
        def _count_connect(dbapi_conn: object, _rec: object) -> None:
            nonlocal connect_count
            connect_count += 1

        _seed_tenant(create_session_factory(engine))
        factory = create_session_factory(engine)
        repo = JobRepository(factory)

        # Multiple sequential checkouts on the same engine.
        for i in range(4):
            _add_job(
                factory,
                job_id=f"pool-reuse-{i}",
                priority=i,
                run_after=T0,
                created_at=T0 + timedelta(seconds=i),
            )
            claim = repo.claim_next(
                role=JobRole.CORE,
                owner=f"w{i}",
                lease_ttl=LEASE_TTL,
                now=T0,
            )
            assert claim is not None
            assert claim.job_id == f"pool-reuse-{i}"

        # At most one physical connection opened (first checkout); subsequent
        # checkouts reuse the pooled connection.
        assert connect_count == 1
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Held-write-lock contention → None then success after release
# ---------------------------------------------------------------------------


def test_claim_next_returns_none_under_bounded_contention(concurrency_db: str) -> None:
    """claim_next returns None under held write lock, succeeds after release.

    One engine holds an uncommitted write lock on the target row while a
    second engine's claim_next races.  The 0.25 s claim budget forces the
    racing engine to return None.  After the lock is released, claim_next
    succeeds.
    """
    settings = Settings(database_url=concurrency_db)
    seed_engine = create_db_engine(settings)
    seed_factory = create_session_factory(seed_engine)
    _seed_tenant(seed_factory)
    _add_job(
        seed_factory,
        job_id="lock-job",
        priority=1,
        run_after=T0,
        created_at=T0,
    )
    seed_engine.dispose()

    # Holder engine: BEGIN IMMEDIATE + UPDATE the target row, hold open.
    holder_engine = create_db_engine(settings)
    holder_conn = holder_engine.raw_connection()
    holder_cursor = holder_conn.cursor()
    holder_cursor.execute("BEGIN IMMEDIATE")
    holder_cursor.execute("UPDATE jobs SET priority = 99 WHERE id = 'lock-job'")

    lock_held = threading.Event()
    lock_released = threading.Event()
    racer_result: list[JobClaim | None] = [None]
    racer_errors: list[BaseException] = []

    def racer() -> None:
        try:
            lock_held.wait(timeout=5)
            engine = create_db_engine(settings)
            factory = create_session_factory(engine)
            repo = JobRepository(factory)
            racer_result[0] = repo.claim_next(
                role=JobRole.CORE,
                owner="racer",
                lease_ttl=LEASE_TTL,
                now=T0,
            )
            engine.dispose()
        except BaseException as exc:
            racer_errors.append(exc)
        finally:
            lock_released.set()

    t = threading.Thread(target=racer, name="held-lock-racer")
    t.start()
    lock_held.set()

    lock_released.wait(timeout=5)
    t.join(timeout=5)
    assert not t.is_alive()
    assert racer_errors == [], f"racer raised: {racer_errors}"
    assert racer_result[0] is None

    # Release lock, then assert claim_next succeeds after release.
    try:
        holder_conn.rollback()
    finally:
        holder_cursor.close()
        holder_conn.close()
        holder_engine.dispose()

    fresh_engine = create_db_engine(settings)
    fresh_factory = create_session_factory(fresh_engine)
    fresh_repo = JobRepository(fresh_factory)
    claim = fresh_repo.claim_next(
        role=JobRole.CORE,
        owner="after-release",
        lease_ttl=LEASE_TTL,
        now=T0,
    )
    assert claim is not None
    assert claim.job_id == "lock-job"
    fresh_engine.dispose()


# ---------------------------------------------------------------------------
# Non-lock error propagation
# ---------------------------------------------------------------------------


def test_nonlock_error_propagates_through_short_tx(concurrency_db: str) -> None:
    """Non-lock errors (e.g. programming bugs) propagate, not swallowed."""
    settings = Settings(database_url=concurrency_db)
    engine = create_db_engine(settings)
    try:
        factory = create_session_factory(engine)
        repo = JobRepository(factory)

        with pytest.raises(ValueError, match="limit must be >= 1"):
            repo.discover_due_candidates(role=JobRole.CORE, now=T0, limit=0)

        # Inject a non-lock error into a short tx to prove it propagates.
        def _boom(session: Session) -> None:
            raise ValueError("artificial non-lock error")

        with pytest.raises(ValueError, match="artificial non-lock error"):
            repo._run_short_tx(_boom)
    finally:
        engine.dispose()


def test_run_short_tx_rejects_nonpositive_retry_budget(concurrency_db: str) -> None:
    """_run_short_tx raises ValueError for retry_budget_s <= 0."""
    settings = Settings(database_url=concurrency_db)
    engine = create_db_engine(settings)
    try:
        factory = create_session_factory(engine)
        repo = JobRepository(factory)

        def _noop(session: Session) -> None:
            return None

        with pytest.raises(ValueError, match="retry_budget_s must be > 0"):
            repo._run_short_tx(_noop, retry_budget_s=0)
        with pytest.raises(ValueError, match="retry_budget_s must be > 0"):
            repo._run_short_tx(_noop, retry_budget_s=-1.0)
    finally:
        engine.dispose()
