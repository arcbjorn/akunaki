"""Enqueue path: idempotent job creation against file-backed libSQL.

Enqueue is the only way work enters the durable lifecycle, and the design
requires deduplication on ``(tenant_id, idempotency_key)`` so retried API
calls, redelivered webhooks, and re-run schedulers cannot fan out duplicates.
"""

from __future__ import annotations

import threading
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
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import JobRole, JobStatus, to_utc_rfc3339

T0 = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _migrate(database_url: str) -> None:
    cfg = Config(str(_backend_root() / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.set_main_option("script_location", str(_backend_root() / "alembic"))
    clear_settings_cache()
    command.upgrade(cfg, "head")


@pytest.fixture
def enqueue_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "enqueue.db"
    url = f"sqlite+libsql:///{db_path.resolve()}"
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", url)
    clear_settings_cache()
    _migrate(url)
    yield url
    clear_settings_cache()


def _seed_tenant(factory: sessionmaker[Session], tenant_id: str = "tenant-1") -> None:
    with factory() as session, session.begin():
        session.add(
            Tenant(
                id=tenant_id,
                created_at=to_utc_rfc3339(T0),
                status="active",
                primary_timezone="UTC",
                display_name="Test",
            )
        )


@pytest.fixture
def repository(enqueue_db: str) -> Generator[JobRepository]:
    engine = create_db_engine(Settings(database_url=enqueue_db))
    factory = create_session_factory(engine)
    _seed_tenant(factory)
    try:
        yield JobRepository(factory)
    finally:
        engine.dispose()


def _jobs(enqueue_db: str) -> list[Job]:
    engine = create_db_engine(Settings(database_url=enqueue_db))
    try:
        with create_session_factory(engine)() as session:
            rows = list(session.scalars(select(Job).order_by(Job.id)).all())
            for row in rows:
                session.expunge(row)
            return rows
    finally:
        engine.dispose()


def test_enqueue_creates_a_ready_due_job(repository: JobRepository, enqueue_db: str) -> None:
    result = repository.enqueue_job(
        job_id="job-1",
        tenant_id="tenant-1",
        job_type="connection.initial_sync",
        payload_json='{"connection_id":"c1"}',
        now=T0,
    )

    assert result.created is True
    assert result.job_id == "job-1"
    assert result.role is JobRole.CORE

    jobs = _jobs(enqueue_db)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.status == JobStatus.READY.value
    assert job.job_type == "connection.initial_sync"
    assert job.attempts == 0
    assert job.fence_token == 0
    # run_after defaults to now, so the job is immediately claimable.
    assert job.run_after == to_utc_rfc3339(T0)


def test_enqueue_is_idempotent_for_same_key(repository: JobRepository, enqueue_db: str) -> None:
    first = repository.enqueue_job(
        job_id="job-1",
        tenant_id="tenant-1",
        job_type="connection.initial_sync",
        payload_json='{"n":1}',
        now=T0,
        idempotency_key="tenant-1:c1:initial",
    )
    # A retried caller supplies a different job id but the same logical key.
    second = repository.enqueue_job(
        job_id="job-2",
        tenant_id="tenant-1",
        job_type="connection.initial_sync",
        payload_json='{"n":2}',
        now=T0 + timedelta(seconds=5),
        idempotency_key="tenant-1:c1:initial",
    )

    assert first.created is True
    assert second.created is False
    # The caller is pointed at the winning job, not its own rejected id.
    assert second.job_id == "job-1"
    assert len(_jobs(enqueue_db)) == 1


def test_null_key_always_inserts(repository: JobRepository, enqueue_db: str) -> None:
    # SQL NULL never conflicts, so unkeyed jobs are never deduped.
    for i in range(3):
        result = repository.enqueue_job(
            job_id=f"job-{i}",
            tenant_id="tenant-1",
            job_type="day.recompute",
            payload_json="{}",
            now=T0,
        )
        assert result.created is True

    assert len(_jobs(enqueue_db)) == 3


def test_same_key_different_tenants_both_insert(repository: JobRepository, enqueue_db: str) -> None:
    # Dedupe is tenant-scoped; one tenant's key must not block another's.
    engine = create_db_engine(Settings(database_url=enqueue_db))
    try:
        _seed_tenant(create_session_factory(engine), tenant_id="tenant-2")
    finally:
        engine.dispose()

    a = repository.enqueue_job(
        job_id="job-a",
        tenant_id="tenant-1",
        job_type="raw.normalize",
        payload_json="{}",
        now=T0,
        idempotency_key="shared-key",
    )
    b = repository.enqueue_job(
        job_id="job-b",
        tenant_id="tenant-2",
        job_type="raw.normalize",
        payload_json="{}",
        now=T0,
        idempotency_key="shared-key",
    )

    assert a.created is True
    assert b.created is True
    assert len(_jobs(enqueue_db)) == 2


def test_enqueued_job_is_claimable_and_runs(repository: JobRepository) -> None:
    """Enqueue feeds the real claim path: no hand-inserted rows required."""
    repository.enqueue_job(
        job_id="job-1",
        tenant_id="tenant-1",
        job_type="system.noop",
        payload_json='{"kind":"ping"}',
        now=T0,
        idempotency_key="tenant-1:ping",
    )

    claim = repository.claim_next(
        role=JobRole.CORE,
        owner="worker-1",
        lease_ttl=timedelta(seconds=30),
        now=T0,
    )
    assert claim is not None
    assert claim.job_id == "job-1"
    assert claim.job_type == "system.noop"
    assert claim.payload_json == '{"kind":"ping"}'


def test_future_run_after_is_not_yet_due(repository: JobRepository) -> None:
    repository.enqueue_job(
        job_id="job-later",
        tenant_id="tenant-1",
        job_type="system.noop",
        payload_json="{}",
        now=T0,
        run_after=T0 + timedelta(hours=1),
    )

    assert (
        repository.claim_next(
            role=JobRole.CORE,
            owner="worker-1",
            lease_ttl=timedelta(seconds=30),
            now=T0,
        )
        is None
    )
    # Due once the scheduled time arrives.
    later = repository.claim_next(
        role=JobRole.CORE,
        owner="worker-1",
        lease_ttl=timedelta(seconds=30),
        now=T0 + timedelta(hours=2),
    )
    assert later is not None
    assert later.job_id == "job-later"


def test_agent_role_is_not_claimable_by_core(repository: JobRepository) -> None:
    # Agent isolation: a core worker must never claim agent work.
    repository.enqueue_job(
        job_id="job-agent",
        tenant_id="tenant-1",
        job_type="agent.answer",
        payload_json="{}",
        now=T0,
        role=JobRole.AGENT,
    )

    assert (
        repository.claim_next(
            role=JobRole.CORE,
            owner="core-worker",
            lease_ttl=timedelta(seconds=30),
            now=T0,
        )
        is None
    )
    agent_claim = repository.claim_next(
        role=JobRole.AGENT,
        owner="agent-worker",
        lease_ttl=timedelta(seconds=30),
        now=T0,
    )
    assert agent_claim is not None
    assert agent_claim.job_id == "job-agent"


def test_duplicate_job_id_without_key_is_rejected(repository: JobRepository) -> None:
    repository.enqueue_job(
        job_id="job-1",
        tenant_id="tenant-1",
        job_type="system.noop",
        payload_json="{}",
        now=T0,
    )
    # Without an idempotency key a repeated id is a caller bug, not a dedupe.
    with pytest.raises(ValueError, match="already exists"):
        repository.enqueue_job(
            job_id="job-1",
            tenant_id="tenant-1",
            job_type="system.noop",
            payload_json="{}",
            now=T0,
        )


def test_enqueue_validates_arguments(repository: JobRepository) -> None:
    base = {
        "job_id": "job-1",
        "tenant_id": "tenant-1",
        "job_type": "system.noop",
        "payload_json": "{}",
        "now": T0,
    }
    for field in ("job_id", "tenant_id", "job_type", "payload_json"):
        with pytest.raises(ValueError, match=f"{field} must be non-empty"):
            repository.enqueue_job(**{**base, field: ""})  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="max_attempts must be >= 1"):
        repository.enqueue_job(**base, max_attempts=0)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="idempotency_key must be non-empty"):
        repository.enqueue_job(**base, idempotency_key="")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="must be timezone-aware"):
        repository.enqueue_job(
            **{**base, "now": datetime(2026, 7, 18, 12, 0, 0)},
        )


def test_invalid_payload_json_is_rejected_by_schema(repository: JobRepository) -> None:
    # The jobs table enforces json_valid(payload_json); enqueue must not
    # silently persist a malformed payload.
    with pytest.raises(Exception, match=r"(?i)constraint|json"):
        repository.enqueue_job(
            job_id="job-bad",
            tenant_id="tenant-1",
            job_type="system.noop",
            payload_json="not json",
            now=T0,
        )


def test_concurrent_enqueue_of_same_key_inserts_once(enqueue_db: str) -> None:
    """Racing enqueues dedupe atomically: one insert, no exceptions.

    A check-then-insert implementation would either double-insert or raise a
    constraint error on the loser; ON CONFLICT DO NOTHING must do neither.
    """
    n_threads = 4
    engine = create_db_engine(Settings(database_url=enqueue_db))
    try:
        _seed_tenant(create_session_factory(engine))
    finally:
        engine.dispose()

    barrier = threading.Barrier(n_threads)
    created_flags: list[bool] = []
    winning_ids: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def enqueue(worker_id: int) -> None:
        worker_engine = create_db_engine(Settings(database_url=enqueue_db))
        try:
            repo = JobRepository(create_session_factory(worker_engine))
            barrier.wait(timeout=10)
            result = repo.enqueue_job(
                job_id=f"job-{worker_id}",
                tenant_id="tenant-1",
                job_type="connection.incremental_sync",
                payload_json="{}",
                now=T0,
                idempotency_key="tenant-1:c1:window",
            )
            with lock:
                created_flags.append(result.created)
                winning_ids.append(result.job_id)
        except BaseException as exc:
            errors.append(exc)
        finally:
            worker_engine.dispose()

    threads = [
        threading.Thread(target=enqueue, args=(i,), name=f"enq-{i}") for i in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), f"thread {t.name} still alive after join"

    assert not errors, f"enqueue errors: {errors}"
    # Exactly one caller created the job; all agree on the winner.
    assert created_flags.count(True) == 1
    assert len(set(winning_ids)) == 1
    assert len(_jobs(enqueue_db)) == 1
