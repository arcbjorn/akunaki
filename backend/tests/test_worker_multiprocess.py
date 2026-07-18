"""Concurrent worker runtimes against one file-backed libSQL database.

The runtime policy tests use a fake repository and the end-to-end tests drive
a single worker. These prove the *runtime* honors fencing when several workers
compete: exactly-once handler execution, single-leader reaping, and no false
success when a lease is stolen mid-flight.

Bounded stress shape matching the repository concurrency suite: fixed job
count, one independent engine per worker, barrier start. Fairness (every
worker winning some job) is not a CAS guarantee and is never asserted.
"""

from __future__ import annotations

import threading
from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.job_repository import JobRepository
from akunaki.adapters.db.models import Job, JobAttempt, JobLease, LeaderLease, Tenant
from akunaki.application.handlers import NOOP_JOB_TYPE, HandlerRegistry
from akunaki.application.worker_runtime import JobWorker, WorkerConfig
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import JobClaim, JobRole, JobStatus, to_utc_rfc3339

T0 = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)
LEASE_TTL = timedelta(seconds=30)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _migrate(database_url: str) -> None:
    cfg = Config(str(_backend_root() / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.set_main_option("script_location", str(_backend_root() / "alembic"))
    clear_settings_cache()
    command.upgrade(cfg, "head")


@pytest.fixture
def fleet_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "fleet.db"
    url = f"sqlite+libsql:///{db_path.resolve()}"
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", url)
    clear_settings_cache()
    _migrate(url)
    yield url
    clear_settings_cache()


def _seed(database_url: str, *, n_jobs: int, job_type: str = NOOP_JOB_TYPE) -> None:
    engine = create_db_engine(Settings(database_url=database_url))
    factory = create_session_factory(engine)
    now_s = to_utc_rfc3339(T0)
    try:
        with factory() as session, session.begin():
            session.add(
                Tenant(
                    id="tenant-1",
                    created_at=now_s,
                    status="active",
                    primary_timezone="UTC",
                    display_name="Test",
                )
            )
            for i in range(n_jobs):
                session.add(
                    Job(
                        id=f"job-{i:03d}",
                        tenant_id="tenant-1",
                        role=JobRole.CORE.value,
                        status=JobStatus.READY.value,
                        payload_json='{"kind":"ping"}',
                        priority=i % 5,
                        run_after=now_s,
                        attempts=0,
                        max_attempts=3,
                        idempotency_key=f"job-{i:03d}",
                        fence_token=0,
                        created_at=now_s,
                        updated_at=now_s,
                        job_type=job_type,
                    )
                )
    finally:
        engine.dispose()


def _read(database_url: str, fn):  # type: ignore[no-untyped-def]
    engine = create_db_engine(Settings(database_url=database_url))
    try:
        with create_session_factory(engine)() as session:
            return fn(session)
    finally:
        engine.dispose()


def test_competing_workers_execute_each_job_exactly_once(fleet_db: str) -> None:
    """Concurrent runtimes drain a queue with no duplicate or lost execution."""
    n_jobs = 24
    n_workers = 3
    _seed(fleet_db, n_jobs=n_jobs)

    executed: list[str] = []
    lock = threading.Lock()
    errors: list[BaseException] = []
    barrier = threading.Barrier(n_workers)

    def run_worker(worker_id: int) -> None:
        engine = create_db_engine(Settings(database_url=fleet_db))
        try:

            def handler(claim: JobClaim) -> None:
                with lock:
                    executed.append(claim.job_id)

            worker = JobWorker(
                JobRepository(create_session_factory(engine)),
                owner=f"worker-{worker_id}",
                config=WorkerConfig(lease_ttl=LEASE_TTL),
                registry=HandlerRegistry({NOOP_JOB_TYPE: handler}),
                clock=lambda: T0,
                sleep=lambda _s: None,
                jitter=lambda: 0.0,
            )
            barrier.wait(timeout=10)
            # Drain until repeatedly empty; another worker may finish last.
            empty_streak = 0
            while empty_streak < 3:
                if worker.run_once():
                    empty_streak = 0
                else:
                    empty_streak += 1
        except BaseException as exc:
            errors.append(exc)
        finally:
            engine.dispose()

    threads = [
        threading.Thread(target=run_worker, args=(i,), name=f"w{i}") for i in range(n_workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive(), f"thread {t.name} still alive after join"
    assert not errors, f"worker errors: {errors}"

    # Every job executed exactly once by exactly one worker.
    assert len(executed) == n_jobs
    assert len(set(executed)) == n_jobs

    def check(session: Session) -> None:
        jobs = session.scalars(select(Job)).all()
        assert len(jobs) == n_jobs
        assert all(j.status == JobStatus.SUCCEEDED.value for j in jobs)
        assert all(j.attempts == 1 for j in jobs)
        # One durable attempt per job, all succeeded.
        attempts = session.scalars(select(JobAttempt)).all()
        assert len(attempts) == n_jobs
        assert all(a.status == "succeeded" for a in attempts)

    _read(fleet_db, check)


def test_only_one_worker_holds_the_reaper_lease(fleet_db: str) -> None:
    """Concurrent reaper ticks yield a single leader; standbys never reap."""
    n_workers = 4
    _seed(fleet_db, n_jobs=0)

    leaders: list[str] = []
    lock = threading.Lock()
    errors: list[BaseException] = []
    barrier = threading.Barrier(n_workers)

    def tick(worker_id: int) -> None:
        engine = create_db_engine(Settings(database_url=fleet_db))
        try:
            worker = JobWorker(
                JobRepository(create_session_factory(engine)),
                owner=f"worker-{worker_id}",
                config=WorkerConfig(lease_ttl=LEASE_TTL),
                clock=lambda: T0,
                sleep=lambda _s: None,
                jitter=lambda: 0.0,
            )
            barrier.wait(timeout=10)
            worker.run_once()
            # Reaper counters only advance for the leadership holder.
            if worker.stats.requeued_expired or worker.stats.dead_lettered_expired:
                with lock:
                    leaders.append(worker.owner)
        except BaseException as exc:
            errors.append(exc)
        finally:
            engine.dispose()

    threads = [threading.Thread(target=tick, args=(i,), name=f"r{i}") for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive(), f"thread {t.name} still alive after join"
    assert not errors, f"reaper errors: {errors}"

    # Exactly one leader lease row, held by one owner.
    def check(session: Session) -> None:
        rows = session.scalars(select(LeaderLease)).all()
        assert len(rows) == 1
        assert rows[0].lease_name == "core-reaper"
        assert rows[0].lease_owner.startswith("worker-")

    _read(fleet_db, check)


@dataclass
class _StolenLeaseTrace:
    """What the worker actually did while its lease was being stolen."""

    heartbeats: list[bool] = field(default_factory=list)
    complete_attempts: int = 0


def _run_stolen_lease_scenario(
    fleet_db: str,
    *,
    heartbeat_interval: timedelta,
    await_heartbeat_rejection: bool,
) -> tuple[JobWorker, _StolenLeaseTrace]:
    """Claim a job, let a reaper steal it mid-handler, then let the handler finish.

    ``await_heartbeat_rejection`` holds the handler until a heartbeat has
    actually observed the theft, which makes the runtime-guard path
    deterministic instead of racing the reaper. Returns the worker plus a trace
    of heartbeat outcomes and completion attempts, so each caller can assert on
    the specific mechanism it is exercising.
    """
    _seed(fleet_db, n_jobs=1)
    engine = create_db_engine(Settings(database_url=fleet_db))
    reaper_engine = create_db_engine(Settings(database_url=fleet_db))
    claimed = threading.Event()
    reaped = threading.Event()
    heartbeat_rejected = threading.Event()
    trace = _StolenLeaseTrace()

    class _RecordingRepository(JobRepository):
        """Records heartbeat and completion calls so each path is observable."""

        def heartbeat_job(self, **kwargs) -> bool:  # type: ignore[no-untyped-def]
            alive = super().heartbeat_job(**kwargs)
            trace.heartbeats.append(alive)
            if not alive:
                heartbeat_rejected.set()
            return alive

        def complete_job(self, **kwargs) -> bool:  # type: ignore[no-untyped-def]
            # Whether this is even reached is what distinguishes the runtime
            # guard from the durable fence backstop.
            trace.complete_attempts += 1
            return super().complete_job(**kwargs)

    try:
        repository = _RecordingRepository(create_session_factory(engine))

        def slow_handler(_claim: JobClaim) -> None:
            claimed.set()
            # Hold the job until the reaper has stolen it from this worker.
            assert reaped.wait(timeout=30), "reaper did not run"
            if await_heartbeat_rejection:
                # Keep holding until a heartbeat has seen the loss, so the
                # runtime guard (not the fence) is what stops completion.
                assert heartbeat_rejected.wait(timeout=30), "heartbeat never rejected"

        worker = JobWorker(
            repository,
            owner="worker-slow",
            config=WorkerConfig(
                # Long enough that both a fast and a slow heartbeat interval
                # are valid; the stolen lease comes from the reaper's clock,
                # not from a short TTL.
                lease_ttl=timedelta(seconds=60),
                heartbeat_interval=heartbeat_interval,
            ),
            registry=HandlerRegistry({NOOP_JOB_TYPE: slow_handler}),
            # Clock past the lease horizon, so heartbeats are genuinely
            # rejected instead of silently renewing a stolen lease.
            clock=lambda: T0 + timedelta(minutes=5),
            sleep=lambda _s: None,
            jitter=lambda: 0.0,
        )

        thread = threading.Thread(target=worker.run_once, name="slow-worker")
        thread.start()
        assert claimed.wait(timeout=30), "worker never claimed the job"

        # A second worker reaps the expired lease well after the horizon.
        reaper_repo = JobRepository(create_session_factory(reaper_engine))
        # Past the claimed lease horizon (worker clock + 60s TTL).
        requeued = reaper_repo.requeue_expired_leases(now=T0 + timedelta(minutes=30))
        assert requeued == 1
        reaped.set()

        thread.join(timeout=30)
        assert not thread.is_alive()
        return worker, trace
    finally:
        reaped.set()
        engine.dispose()
        reaper_engine.dispose()


def test_heartbeat_observes_stolen_lease_and_blocks_completion(fleet_db: str) -> None:
    """The runtime's own guard catches the theft before completion is attempted.

    Heartbeats run often enough to observe the rejection, so the worker skips
    ``complete_job`` rather than leaning on the repository fence.
    """
    worker, trace = _run_stolen_lease_scenario(
        fleet_db,
        heartbeat_interval=timedelta(milliseconds=50),
        await_heartbeat_rejection=True,
    )

    # The guard is only meaningful if a heartbeat actually reported the loss.
    assert trace.heartbeats, "heartbeat never ran; the runtime guard was not exercised"
    assert trace.heartbeats[-1] is False
    # The distinguishing observable: the guard stops the worker *before* it
    # ever asks the repository to complete a job it no longer owns.
    assert trace.complete_attempts == 0
    assert worker.stats.succeeded == 0
    assert worker.stats.lease_lost == 1


def test_repository_fence_rejects_completion_when_heartbeat_misses_theft(
    fleet_db: str,
) -> None:
    """Backstop: with no heartbeat in time, the fenced completion still fails.

    Defense in depth — even when the runtime guard never fires, the durable
    fence must refuse to mark a stolen job succeeded.
    """
    worker, trace = _run_stolen_lease_scenario(
        fleet_db,
        # Longer than the handler lives, so no heartbeat observes the theft.
        heartbeat_interval=timedelta(seconds=30),
        await_heartbeat_rejection=False,
    )

    assert trace.heartbeats == [], "heartbeat ran; this test must exercise the fence path"
    # Completion really was attempted here; the durable fence is what refused it.
    assert trace.complete_attempts == 1
    assert worker.stats.succeeded == 0
    assert worker.stats.lease_lost == 1

    def check(session: Session) -> None:
        job = session.get(Job, "job-000")
        assert job is not None
        # Requeued and available again, not falsely succeeded.
        assert job.status == JobStatus.READY.value
        assert session.get(JobLease, "job-000") is None

    _read(fleet_db, check)
