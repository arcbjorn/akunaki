"""Age-based retention for operational history: settled jobs and sync runs.

Each grew by ~700 rows a day with nothing deleting them. The properties that
make the stores safe to run unattended: a job still in flight is never touched
whatever its age, failures keep longer than successes, and a sync run that
retained data still cites is never deleted.
"""

from __future__ import annotations

import json
from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.crypto.envelope import KEY_BYTES, EnvelopeSealer
from akunaki.adapters.db.connection_repository import ConnectionRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import Job, JobAttempt, RawPayload, SyncRun, Tenant
from akunaki.adapters.db.retention_repository import SettledJobRetention, SyncRunRetention
from akunaki.adapters.db.sync_run_repository import SyncRunRepository
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.connections import Provider
from akunaki.domain.jobs import JobStatus, to_utc_rfc3339
from akunaki.domain.sync_runs import SyncRunStatus, SyncRunTrigger
from conftest import upgrade_to_head

T0 = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
KEK = b"\x55" * KEY_BYTES


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "retention.db"
    url = f"sqlite+libsql:///{db_path.resolve()}"
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", url)
    clear_settings_cache()
    upgrade_to_head(url)
    yield url
    clear_settings_cache()


@pytest.fixture
def factory(db_url: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=db_url))
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
    sealer = EnvelopeSealer(keys={"v1": KEK}, active_key_version="v1")
    ConnectionRepository(session_factory).link(
        connection_id="conn-1",
        tenant_id="tenant-1",
        provider=Provider.POLAR,
        sealed_secret=sealer.seal(b'{"access_token":"at"}', aad=b"conn-1"),
        scopes=(),
        external_user_id=None,
        now=T0,
    )
    try:
        yield session_factory
    finally:
        engine.dispose()


def _job(factory: sessionmaker[Session], *, job_id: str, status: JobStatus, age: timedelta) -> None:
    at = to_utc_rfc3339(T0 - age)
    with factory() as session, session.begin():
        session.add(
            Job(
                id=job_id,
                tenant_id="tenant-1",
                role="core",
                status=status.value,
                payload_json="{}",
                priority=100,
                run_after=at,
                attempts=1,
                max_attempts=5,
                idempotency_key=None,
                fence_token=1,
                created_at=at,
                updated_at=at,
                job_type="connection.incremental_sync",
            )
        )
        session.add(
            JobAttempt(
                id=f"{job_id}-attempt",
                job_id=job_id,
                attempt_number=1,
                fence_token=1,
                lease_owner="worker-1",
                status="succeeded" if status is JobStatus.SUCCEEDED else "dead_letter",
                error_class=None,
                redacted_error_message=None,
                started_at=at,
                finished_at=at,
            )
        )


def _job_ids(factory: sessionmaker[Session]) -> set[str]:
    with factory() as session:
        return {job.id for job in session.scalars(select(Job)).all()}


def test_settled_jobs_go_after_thirty_days_with_their_attempts(
    factory: sessionmaker[Session],
) -> None:
    _job(factory, job_id="old-ok", status=JobStatus.SUCCEEDED, age=timedelta(days=31))
    _job(factory, job_id="old-cancelled", status=JobStatus.CANCELLED, age=timedelta(days=31))
    _job(factory, job_id="young-ok", status=JobStatus.SUCCEEDED, age=timedelta(days=29))

    removed = SettledJobRetention(factory).purge_expired(now=T0)

    assert removed == 2
    assert _job_ids(factory) == {"young-ok"}
    with factory() as session:
        attempts = {a.job_id for a in session.scalars(select(JobAttempt)).all()}
    assert attempts == {"young-ok"}


def test_failures_keep_ninety_days_and_live_jobs_are_never_touched(
    factory: sessionmaker[Session],
) -> None:
    """The dead letters are the diagnostics; a job still queued is work, not history."""
    _job(factory, job_id="dead-60d", status=JobStatus.DEAD_LETTER, age=timedelta(days=60))
    _job(factory, job_id="dead-91d", status=JobStatus.DEAD_LETTER, age=timedelta(days=91))
    _job(factory, job_id="failed-91d", status=JobStatus.FAILED, age=timedelta(days=91))
    _job(factory, job_id="ready-old", status=JobStatus.READY, age=timedelta(days=400))
    _job(factory, job_id="leased-old", status=JobStatus.LEASED, age=timedelta(days=400))

    removed = SettledJobRetention(factory).purge_expired(now=T0)

    assert removed == 2
    assert _job_ids(factory) == {"dead-60d", "ready-old", "leased-old"}


def _run(
    factory: sessionmaker[Session], *, run_id: str, age: timedelta, status: SyncRunStatus
) -> None:
    runs = SyncRunRepository(factory)
    started = T0 - age
    runs.open(
        run_id=run_id,
        tenant_id="tenant-1",
        connection_id="conn-1",
        trigger=SyncRunTrigger.SCHEDULE,
        stream="workout",
        now=started,
    )
    if status is not SyncRunStatus.RUNNING:
        runs.close(run_id=run_id, status=status, now=started + timedelta(seconds=5))


def _cite(factory: sessionmaker[Session], *, run_id: str) -> None:
    with factory() as session, session.begin():
        session.add(
            RawPayload(
                id=f"payload-{run_id}",
                tenant_id="tenant-1",
                connection_id="conn-1",
                sync_run_id=run_id,
                transport_kind="sync_fetch",
                provider="polar",
                stream="workout",
                page_token=None,
                fetched_at=NOW_S,
                received_at=NOW_S,
                http_status=200,
                content_type="application/json",
                content_hash="h1",
                payload_json=json.dumps({"data": []}),
                payload_blob=None,
                request_meta_json="{}",
            )
        )


def test_old_uncited_sync_runs_go_and_cited_or_running_ones_stay(
    factory: sessionmaker[Session],
) -> None:
    """A run that retained data cites is lineage, not housekeeping's to cut."""
    _run(factory, run_id="old-empty", age=timedelta(days=91), status=SyncRunStatus.SUCCEEDED)
    _run(factory, run_id="old-failed", age=timedelta(days=91), status=SyncRunStatus.FAILED)
    _run(factory, run_id="old-cited", age=timedelta(days=91), status=SyncRunStatus.SUCCEEDED)
    _cite(factory, run_id="old-cited")
    _run(factory, run_id="old-running", age=timedelta(days=91), status=SyncRunStatus.RUNNING)
    _run(factory, run_id="young", age=timedelta(days=10), status=SyncRunStatus.SUCCEEDED)

    removed = SyncRunRetention(factory).purge_expired(now=T0)

    assert removed == 2
    with factory() as session:
        remaining = {run.id for run in session.scalars(select(SyncRun)).all()}
    assert remaining == {"old-cited", "old-running", "young"}
