"""Privacy deletion: ordering, scrub completeness, and proof minimality.

Ordering is a safety property — jobs must be cancelled before rows are
scrubbed, or an in-flight sync could re-insert data that was just deleted.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.crypto.envelope import KEY_BYTES, EnvelopeSealer
from akunaki.adapters.db.connection_repository import ConnectionRepository
from akunaki.adapters.db.deletion_repository import DeletionRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.fact_repository import FactRepository
from akunaki.adapters.db.job_repository import JobRepository
from akunaki.adapters.db.models import (
    Anomaly,
    Connection,
    ConnectionSecret,
    DailyHealthScore,
    DeletionCompletionProof,
    DeletionRequest,
    FactRecord,
    Job,
    SleepSession,
    SubjectiveCheckIn,
    Tenant,
)
from akunaki.application.deletion_service import DeletionService
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.connections import Provider
from akunaki.domain.deletion import (
    DeletionOrderError,
    DeletionStatus,
    ScrubCounts,
    is_terminal,
    require_transition,
)
from akunaki.domain.jobs import JobStatus, to_utc_rfc3339
from akunaki.domain.sleep_normalizer import normalize_sleep_payload
from conftest import upgrade_to_head

_AUDIT_IDS = itertools.count(1)

T0 = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
DAY = "2026-07-19"
KEK = b"\x77" * KEY_BYTES

SLEEP_RECORD = json.dumps(
    {
        "data": [
            {
                "id": "sleep-1",
                "bedtime_start": "2026-07-18T23:00:00+02:00",
                "bedtime_end": "2026-07-19T07:00:00+02:00",
                "total_sleep_duration": 27000,
                "type": "long_sleep",
            }
        ]
    }
)


@pytest.fixture
def del_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "deletion.db"
    url = f"sqlite+libsql:///{db_path.resolve()}"
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", url)
    clear_settings_cache()
    upgrade_to_head(url)
    yield url
    clear_settings_cache()


@pytest.fixture
def factory(del_db: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=del_db))
    session_factory = create_session_factory(engine)
    for tenant_id in ("tenant-1", "tenant-2"):
        with session_factory() as session, session.begin():
            session.add(
                Tenant(
                    id=tenant_id,
                    created_at=NOW_S,
                    status="active",
                    primary_timezone="UTC",
                    display_name="Test",
                )
            )
    try:
        yield session_factory
    finally:
        engine.dispose()


def _populate(factory: sessionmaker[Session], tenant_id: str) -> None:
    """Give a tenant a connection, sealed tokens, a job, and a sleep fact."""
    sealer = EnvelopeSealer(keys={"v1": KEK}, active_key_version="v1")
    connection_id = f"conn-{tenant_id}"
    ConnectionRepository(factory).link(
        connection_id=connection_id,
        tenant_id=tenant_id,
        provider=Provider.OURA,
        sealed_secret=sealer.seal(
            json.dumps({"access_token": "AT"}).encode(), aad=connection_id.encode()
        ),
        scopes=("daily",),
        external_user_id=None,
        now=T0,
    )
    JobRepository(factory).enqueue_job(
        job_id=f"job-{tenant_id}",
        tenant_id=tenant_id,
        job_type="system.noop",
        payload_json="{}",
        now=T0,
    )
    [fact] = normalize_sleep_payload(SLEEP_RECORD)
    FactRepository(factory).write_sleep_fact(
        fact_record_id=f"fact-{tenant_id}",
        tenant_id=tenant_id,
        connection_id=connection_id,
        fact=fact,
        raw_revision_id=None,
        raw_payload_id=None,
        schema_version="oura.v2",
        now=T0,
    )


def _run_pipeline(
    repository: DeletionRepository,
    *,
    request_id: str = "del-1",
    tenant_id: str = "tenant-1",
) -> ScrubCounts:
    """Drive the full pipeline in its required order."""
    repository.request(request_id=request_id, tenant_id=tenant_id, now=T0)
    cancelled = repository.cancel_jobs(request_id=request_id, now=T0)
    counts = repository.scrub_rows(request_id=request_id, now=T0, jobs_cancelled=cancelled)
    repository.schedule_backup_expiry(request_id=request_id, now=T0)
    repository.complete(
        request_id=request_id, proof_id=f"proof-{request_id}", counts=counts, now=T0
    )
    return counts


# ---------------------------------------------------------------------------
# Ordering (pure)
# ---------------------------------------------------------------------------


def test_pipeline_advances_only_in_order() -> None:
    require_transition(DeletionStatus.REQUESTED, DeletionStatus.JOBS_CANCELLED)
    require_transition(DeletionStatus.JOBS_CANCELLED, DeletionStatus.ROWS_SCRUBBED)
    require_transition(DeletionStatus.ROWS_SCRUBBED, DeletionStatus.BACKUPS_SCHEDULED)
    require_transition(DeletionStatus.BACKUPS_SCHEDULED, DeletionStatus.COMPLETED)


def test_scrubbing_before_cancelling_jobs_is_rejected() -> None:
    """The core safety rule: a running job could re-insert scrubbed rows."""
    with pytest.raises(DeletionOrderError, match="requested"):
        require_transition(DeletionStatus.REQUESTED, DeletionStatus.ROWS_SCRUBBED)


def test_completed_and_failed_are_terminal() -> None:
    assert is_terminal(DeletionStatus.COMPLETED)
    assert is_terminal(DeletionStatus.FAILED)
    with pytest.raises(DeletionOrderError):
        require_transition(DeletionStatus.COMPLETED, DeletionStatus.FAILED)


def test_any_stage_can_fail() -> None:
    for stage in (
        DeletionStatus.REQUESTED,
        DeletionStatus.JOBS_CANCELLED,
        DeletionStatus.ROWS_SCRUBBED,
        DeletionStatus.BACKUPS_SCHEDULED,
    ):
        require_transition(stage, DeletionStatus.FAILED)


# ---------------------------------------------------------------------------
# Ordering (enforced by the repository)
# ---------------------------------------------------------------------------


def test_repository_refuses_to_scrub_before_cancelling(
    factory: sessionmaker[Session],
) -> None:
    _populate(factory, "tenant-1")
    repository = DeletionRepository(factory)
    repository.request(request_id="del-1", tenant_id="tenant-1", now=T0)

    with pytest.raises(DeletionOrderError):
        repository.scrub_rows(request_id="del-1", now=T0, jobs_cancelled=0)

    # Nothing was scrubbed by the rejected attempt.
    with factory() as session:
        assert session.get(Connection, "conn-tenant-1") is not None


def test_repository_refuses_to_complete_early(factory: sessionmaker[Session]) -> None:
    repository = DeletionRepository(factory)
    repository.request(request_id="del-1", tenant_id="tenant-1", now=T0)

    with pytest.raises(DeletionOrderError):
        repository.complete(request_id="del-1", proof_id="p1", counts=ScrubCounts(), now=T0)


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def test_pending_and_leased_jobs_are_cancelled(factory: sessionmaker[Session]) -> None:
    _populate(factory, "tenant-1")
    jobs = JobRepository(factory)
    jobs.enqueue_job(
        job_id="extra",
        tenant_id="tenant-1",
        job_type="system.noop",
        payload_json="{}",
        now=T0,
    )
    # Lease one job so both live states are represented.
    claim = jobs.claim_next(
        role=__import__("akunaki.domain.jobs", fromlist=["JobRole"]).JobRole.CORE,
        owner="worker-1",
        lease_ttl=timedelta(seconds=60),
        now=T0,
    )
    assert claim is not None

    repository = DeletionRepository(factory)
    repository.request(request_id="del-1", tenant_id="tenant-1", now=T0)
    cancelled = repository.cancel_jobs(request_id="del-1", now=T0)

    assert cancelled == 2
    with factory() as session:
        statuses = {
            job.id: job.status
            for job in session.scalars(select(Job).where(Job.tenant_id == "tenant-1")).all()
        }
    assert set(statuses.values()) == {JobStatus.CANCELLED.value}


def test_other_tenants_jobs_are_untouched(factory: sessionmaker[Session]) -> None:
    _populate(factory, "tenant-1")
    _populate(factory, "tenant-2")

    repository = DeletionRepository(factory)
    repository.request(request_id="del-1", tenant_id="tenant-1", now=T0)
    repository.cancel_jobs(request_id="del-1", now=T0)

    with factory() as session:
        other = session.get(Job, "job-tenant-2")
        assert other is not None
        assert other.status == JobStatus.READY.value


# ---------------------------------------------------------------------------
# Scrub
# ---------------------------------------------------------------------------


def test_full_pipeline_scrubs_tenant_data(factory: sessionmaker[Session]) -> None:
    _populate(factory, "tenant-1")
    counts = _run_pipeline(DeletionRepository(factory))

    with factory() as session:
        assert session.get(Tenant, "tenant-1") is None
        assert session.get(Connection, "conn-tenant-1") is None
        assert session.get(ConnectionSecret, "conn-tenant-1") is None
        assert session.get(FactRecord, "fact-tenant-1") is None
        assert session.get(SleepSession, "fact-tenant-1") is None
        assert session.scalars(select(Job).where(Job.tenant_id == "tenant-1")).all() == []

    assert counts.connections == 1
    assert counts.facts == 1
    assert counts.jobs_cancelled == 1


def test_scrub_leaves_other_tenants_intact(factory: sessionmaker[Session]) -> None:
    """A deletion must be surgically scoped to its own tenant."""
    _populate(factory, "tenant-1")
    _populate(factory, "tenant-2")

    _run_pipeline(DeletionRepository(factory))

    with factory() as session:
        assert session.get(Tenant, "tenant-2") is not None
        assert session.get(Connection, "conn-tenant-2") is not None
        assert session.get(ConnectionSecret, "conn-tenant-2") is not None
        assert session.get(FactRecord, "fact-tenant-2") is not None


def test_sealed_tokens_are_hard_deleted(factory: sessionmaker[Session]) -> None:
    """Credentials must not survive a privacy deletion."""
    _populate(factory, "tenant-1")
    _run_pipeline(DeletionRepository(factory))

    with factory() as session:
        assert session.scalars(select(ConnectionSecret)).all() == []


# ---------------------------------------------------------------------------
# Completion proof
# ---------------------------------------------------------------------------


def test_completion_proof_carries_counts_only(factory: sessionmaker[Session]) -> None:
    """The proof must contain no identity and no health values."""
    _populate(factory, "tenant-1")
    _run_pipeline(DeletionRepository(factory))

    with factory() as session:
        proof = session.scalars(select(DeletionCompletionProof)).one()

    rendered = proof.scrub_counts_json
    assert "tenant-1" not in rendered
    assert "Test" not in rendered  # display name
    assert "AT" not in json.dumps(json.loads(rendered))  # token value
    counts = json.loads(rendered)
    assert all(isinstance(value, int) for value in counts.values())
    assert counts["facts"] == 1


def test_request_survives_the_tenant_it_scrubbed(
    factory: sessionmaker[Session],
) -> None:
    """Completing a deletion must not erase its own audit trail."""
    _populate(factory, "tenant-1")
    _run_pipeline(DeletionRepository(factory))

    with factory() as session:
        request = session.get(DeletionRequest, "del-1")
        assert request is not None
        assert request.status == DeletionStatus.COMPLETED.value
        assert session.get(Tenant, "tenant-1") is None


def test_pipeline_timestamps_are_recorded(factory: sessionmaker[Session]) -> None:
    _populate(factory, "tenant-1")
    _run_pipeline(DeletionRepository(factory))

    with factory() as session:
        request = session.get(DeletionRequest, "del-1")
        assert request is not None

    assert request.jobs_cancelled_at is not None
    assert request.rows_scrubbed_at is not None
    assert request.backups_scheduled_at is not None
    assert request.completed_at is not None


def test_status_reporting(factory: sessionmaker[Session]) -> None:
    repository = DeletionRepository(factory)
    assert repository.status_of(request_id="missing") is None

    repository.request(request_id="del-1", tenant_id="tenant-1", now=T0)
    assert repository.status_of(request_id="del-1") is DeletionStatus.REQUESTED


def test_failure_retains_the_stage_reached(factory: sessionmaker[Session]) -> None:
    _populate(factory, "tenant-1")
    repository = DeletionRepository(factory)
    repository.request(request_id="del-1", tenant_id="tenant-1", now=T0)
    repository.cancel_jobs(request_id="del-1", now=T0)
    repository.fail(request_id="del-1", failure_class="scrub_error", now=T0)

    with factory() as session:
        request = session.get(DeletionRequest, "del-1")
        assert request is not None

    assert request.status == DeletionStatus.FAILED.value
    assert request.failure_class == "scrub_error"
    # The cancellation that did happen is still recorded.
    assert request.jobs_cancelled_at is not None


def test_unknown_request_is_rejected(factory: sessionmaker[Session]) -> None:
    with pytest.raises(ValueError, match="not found"):
        DeletionRepository(factory).cancel_jobs(request_id="nope", now=T0)


def _add_health_rows(factory: sessionmaker[Session], tenant_id: str) -> None:
    """Health data in tables the scrub does not name explicitly.

    These arrived with later migrations (vitals, workouts, activity, scores,
    anomalies, check-ins, derivations, selections). Each is tenant-scoped, so a
    deletion must remove every one of them.
    """
    with factory() as session, session.begin():
        session.add(
            SubjectiveCheckIn(
                id=f"ci-{tenant_id}",
                tenant_id=tenant_id,
                local_health_day=DAY,
                energy_n=0.5,
                stress_n=0.5,
                symptom_burden_n=0.1,
                completed_at=NOW_S,
                version_n=1,
                is_current=1,
                superseded_by=None,
                superseded_at=None,
                created_at=NOW_S,
            )
        )
        session.add(
            DailyHealthScore(
                id=f"score-{tenant_id}",
                tenant_id=tenant_id,
                local_health_day=DAY,
                score_code="recovery",
                status="ok",
                score=70,
                available_weight=1.0,
                confidence=0.9,
                formula_version="general_recovery_v0.1.0",
                dependency_hash="hash",
                freshness_at=NOW_S,
                as_of_at=None,
                derivation_run_id=None,
                version_n=1,
                is_current=1,
                superseded_by=None,
                superseded_at=None,
                created_at=NOW_S,
            )
        )
        session.add(
            Anomaly(
                id=f"an-{tenant_id}",
                tenant_id=tenant_id,
                feature_code="hrv",
                started_on=DAY,
                ended_on=None,
                severity="high",
                z_like=-3.0,
                formula_version="general_recovery_v0.1.0",
                is_active=1,
                consecutive_clear_days=0,
                created_at=NOW_S,
                updated_at=NOW_S,
            )
        )


def test_scrub_removes_every_tenant_scoped_health_table(
    factory: sessionmaker[Session],
) -> None:
    """No tenant-scoped health row may survive a privacy deletion.

    The scrub names a fixed model list, but health tables kept arriving with
    later migrations. Any tenant-scoped table not removed — by name or by
    cascade from the tenant row — leaves health data behind after the user
    asked for erasure.
    """
    _populate(factory, "tenant-1")
    _add_health_rows(factory, "tenant-1")

    _run_pipeline(DeletionRepository(factory))

    with factory() as session:
        rows = (
            session.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'alembic%'"
                )
            )
            .scalars()
            .all()
        )
        survivors: dict[str, int] = {}
        for table in rows:
            columns = {r[1] for r in session.execute(text(f"PRAGMA table_info('{table}')")).all()}
            if "tenant_id" not in columns:
                continue
            # The request and its proof deliberately outlive the tenant: they
            # are the audit record that the deletion happened.
            if table in {"deletion_requests", "deletion_completion_proofs"}:
                continue
            left = session.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE tenant_id = 'tenant-1'")  # noqa: S608
            ).scalar_one()
            if left:
                survivors[table] = int(left)

    assert survivors == {}, f"health data survived deletion in: {survivors}"


class _FailingPipeline:
    """A pipeline whose scrub stage raises, to exercise failure handling."""

    def __init__(self) -> None:
        self.failed_as: str | None = None
        self.completed = False

    def request(self, *, request_id: str, tenant_id: str, now: datetime) -> str:
        return request_id

    def cancel_jobs(self, *, request_id: str, now: datetime) -> int:
        return 0

    def scrub_rows(self, *, request_id: str, now: datetime, jobs_cancelled: int) -> ScrubCounts:
        msg = "disk exploded"
        raise RuntimeError(msg)

    def schedule_backup_expiry(self, *, request_id: str, now: datetime) -> None:
        raise AssertionError("must not run after a failed scrub")

    def complete(self, **kwargs: object) -> None:
        self.completed = True

    def fail(self, *, request_id: str, failure_class: str, now: datetime) -> None:
        self.failed_as = failure_class

    def status_of(self, *, request_id: str) -> DeletionStatus | None:
        return None


def test_a_failed_stage_is_recorded_and_never_reads_as_complete() -> None:
    """A half-run deletion must not be reported as done.

    The user asked for erasure; silently completing a pipeline that failed
    mid-scrub would claim data was removed when some of it remains.
    """
    pipeline = _FailingPipeline()
    service = DeletionService(pipeline=pipeline, new_id=lambda: "req-1")

    with pytest.raises(RuntimeError, match="disk exploded"):
        service.delete_tenant(tenant_id="tenant-1", now=T0)

    assert pipeline.failed_as == "RuntimeError"
    assert pipeline.completed is False


class _FailingAudit:
    """An audit sink that always raises."""

    def record(self, **kwargs: object) -> str:
        msg = "audit store unavailable"
        raise RuntimeError(msg)


def test_audit_failure_does_not_fail_a_completed_deletion(
    factory: sessionmaker[Session],
) -> None:
    """The erasure already happened; raising would report a false failure.

    The tenant's data is gone either way, so a broken audit sink must be a loud
    log rather than an exception that tells the caller nothing was deleted.
    """
    _populate(factory, "tenant-1")
    service = DeletionService(
        pipeline=DeletionRepository(factory),
        new_id=lambda: f"id-{next(_AUDIT_IDS)}",
        audit=_FailingAudit(),
    )

    outcome = service.delete_tenant(tenant_id="tenant-1", now=T0)

    assert outcome.status is DeletionStatus.COMPLETED
    with factory() as session:
        assert session.get(Tenant, "tenant-1") is None
