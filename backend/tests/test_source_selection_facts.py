"""Source-selection behavior of the sleep feature queries.

When more than one provider supplies sleep for the same local day, the fact
repository reads only the **authoritative** provider's sessions (Oura over
Google Health), never summing or blending them. These drive the real repository
against a migrated database.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Generator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.fact_repository import FactRepository
from akunaki.adapters.db.models import (
    Connection,
    FactRecord,
    RawObject,
    RawPayload,
    RawRevision,
    SleepSession,
    SourceSelection,
    SourceSelectionCandidate,
    Tenant,
)
from akunaki.adapters.db.source_selection_repository import (
    SelectionWritten,
    SourceSelectionRepository,
)
from akunaki.application.sync_handlers import NORMALIZE_JOB_TYPE, NormalizeHandler
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import EnqueuedJob, JobClaim, JobRole, to_utc_rfc3339
from akunaki.domain.source_policy import SOURCE_POLICY_VERSION, DailySelectionSpec
from akunaki.ports.facts import RevisionBody
from conftest import upgrade_to_head

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
DAY = "2026-07-20"


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "source_selection.db"
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
    try:
        yield session_factory
    finally:
        engine.dispose()


def _seed_sleep(
    factory: sessionmaker[Session],
    *,
    provider: str,
    fact_id: str,
    duration_min: float,
    time_in_bed_min: float | None,
    start_utc: str = NOW_S,
    day: str = DAY,
    tenant_id: str = "tenant-1",
) -> None:
    with factory() as session, session.begin():
        session.add(
            FactRecord(
                id=fact_id,
                tenant_id=tenant_id,
                connection_id=None,
                provider=provider,
                entity_type="sleep_session",
                vendor_record_id=fact_id,
                origin=None,
                method="wearable",
                utc_instant=start_utc,
                start_utc=start_utc,
                end_utc=start_utc,
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
                normalizer_version="n",
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
                tenant_id=tenant_id,
                is_nap=0,
                duration_min=duration_min,
                time_in_bed_min=time_in_bed_min,
                efficiency_pct=None,
                light_min=None,
                deep_min=None,
                rem_min=None,
                awake_min=None,
            )
        )


def test_duration_uses_oura_over_google_health(factory: sessionmaker[Session]) -> None:
    # Both providers cover the night; the sum must NOT double-count.
    _seed_sleep(factory, provider="oura", fact_id="o", duration_min=420.0, time_in_bed_min=460.0)
    _seed_sleep(
        factory, provider="google_health", fact_id="g", duration_min=400.0, time_in_bed_min=440.0
    )

    durations = FactRepository(factory).daily_sleep_durations(
        tenant_id="tenant-1", local_health_days=[DAY]
    )
    # Oura wins: 420, not 820 (sum) and not 400 (Google).
    assert durations[DAY] == pytest.approx(420.0)


def test_duration_falls_back_to_google_health(factory: sessionmaker[Session]) -> None:
    _seed_sleep(
        factory, provider="google_health", fact_id="g", duration_min=400.0, time_in_bed_min=440.0
    )
    durations = FactRepository(factory).daily_sleep_durations(
        tenant_id="tenant-1", local_health_days=[DAY]
    )
    assert durations[DAY] == pytest.approx(400.0)


def test_efficiency_uses_only_the_authoritative_provider(
    factory: sessionmaker[Session],
) -> None:
    # Oura defines efficiency; Google Health's differing ratio is not mixed in.
    _seed_sleep(factory, provider="oura", fact_id="o", duration_min=440.0, time_in_bed_min=460.0)
    _seed_sleep(
        factory, provider="google_health", fact_id="g", duration_min=300.0, time_in_bed_min=600.0
    )
    eff = FactRepository(factory).daily_sleep_efficiency(
        tenant_id="tenant-1", local_health_days=[DAY]
    )
    # 440 / 460 * 100, not a blend with Google's 300/600.
    assert eff[DAY] == pytest.approx(440.0 / 460.0 * 100.0)


def test_efficiency_omitted_when_authoritative_provider_lacks_in_bed(
    factory: sessionmaker[Session],
) -> None:
    # Oura is authoritative but has no in-bed minutes -> undefined; Google's
    # complete data does NOT rescue the day (no cross-provider fallback).
    _seed_sleep(factory, provider="oura", fact_id="o", duration_min=440.0, time_in_bed_min=None)
    _seed_sleep(
        factory, provider="google_health", fact_id="g", duration_min=300.0, time_in_bed_min=600.0
    )
    eff = FactRepository(factory).daily_sleep_efficiency(
        tenant_id="tenant-1", local_health_days=[DAY]
    )
    assert DAY not in eff


def test_midpoint_uses_only_the_authoritative_provider(
    factory: sessionmaker[Session],
) -> None:
    # A longer Google-Health session must not become the principal one over
    # Oura's shorter authoritative session.
    _seed_sleep(
        factory,
        provider="oura",
        fact_id="o",
        duration_min=420.0,
        time_in_bed_min=460.0,
        start_utc="2026-07-19T23:00:00Z",
    )
    _seed_sleep(
        factory,
        provider="google_health",
        fact_id="g",
        duration_min=600.0,
        time_in_bed_min=620.0,
        start_utc="2026-07-19T20:00:00Z",
    )
    mids = FactRepository(factory).daily_principal_sleep_midpoint(
        tenant_id="tenant-1", local_health_days=[DAY]
    )
    # Oura onset 23:00 (1380 min) + duration/2 (210) = 1590, wrapped to 150 on
    # the [0, 1440) circle. Google's 20:00 onset would give a different midpoint;
    # it must not be chosen.
    assert mids[DAY] == pytest.approx((1380.0 + 210.0) % 1440.0)


def test_fact_ids_for_day_pick_the_authoritative_provider(
    factory: sessionmaker[Session],
) -> None:
    """Provenance names only the facts the score actually read.

    A day covered by two sleep providers has one authoritative source; the
    losing candidate's fact must not read as a derivation input, or the lineage
    would claim a value the score never used.
    """
    _seed_sleep(factory, provider="oura", fact_id="o", duration_min=420.0, time_in_bed_min=460.0)
    _seed_sleep(
        factory, provider="google_health", fact_id="g", duration_min=400.0, time_in_bed_min=440.0
    )

    ids = FactRepository(factory).fact_ids_for_day(tenant_id="tenant-1", local_health_day=DAY)

    # Oura wins the day, exactly as the duration/efficiency queries select.
    assert ids == {"sleep_session": ["o"]}


def test_fact_ids_for_day_are_tenant_and_day_scoped(
    factory: sessionmaker[Session],
) -> None:
    """One tenant's facts never read as another's inputs, nor another day's."""
    _seed_sleep(factory, provider="oura", fact_id="o", duration_min=420.0, time_in_bed_min=460.0)
    # A second tenant that genuinely owns a fact on the same day.
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
    _seed_sleep(
        factory,
        provider="oura",
        fact_id="other-tenant-fact",
        duration_min=400.0,
        time_in_bed_min=430.0,
        tenant_id="tenant-2",
    )

    repo = FactRepository(factory)
    # Each tenant sees only its own fact, never the other's.
    assert repo.fact_ids_for_day(tenant_id="tenant-1", local_health_day=DAY) == {
        "sleep_session": ["o"]
    }
    assert repo.fact_ids_for_day(tenant_id="tenant-2", local_health_day=DAY) == {
        "sleep_session": ["other-tenant-fact"]
    }
    # A different day carries none of them.
    assert repo.fact_ids_for_day(tenant_id="tenant-1", local_health_day="2026-07-19") == {}


def test_fact_ids_for_day_empty_when_nothing_recorded(
    factory: sessionmaker[Session],
) -> None:
    repo = FactRepository(factory)
    assert repo.fact_ids_for_day(tenant_id="tenant-1", local_health_day=DAY) == {}


# ---------------------------------------------------------------------------
# Recording the decision (the "Why"), not just applying it
# ---------------------------------------------------------------------------

_OURA_PAGE = json.dumps(
    {
        "data": [
            {
                "id": "oura-sleep-1",
                "type": "long_sleep",
                "bedtime_start": "2026-07-19T23:00:00+00:00",
                "bedtime_end": "2026-07-20T07:00:00+00:00",
                "total_sleep_duration": 25200,
                "time_in_bed": 28800,
                "average_hrv": 60,
                "lowest_heart_rate": 50,
            }
        ]
    }
)


class _StubRevisions:
    """Serves one immutable Oura sleep revision."""

    def get_revision(self, *, revision_id: str) -> RevisionBody:
        return RevisionBody(
            revision_id=revision_id,
            connection_id="conn-1",
            raw_payload_id="pay-1",
            schema_version="oura.v2",
            payload_text=_OURA_PAGE,
            is_tombstone=False,
        )


class _StubJobs:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    def enqueue_job(
        self,
        *,
        job_id: str,
        tenant_id: str,
        job_type: str,
        payload_json: str,
        now: datetime,
        role: JobRole = JobRole.CORE,
        priority: int = 100,
        run_after: datetime | None = None,
        max_attempts: int = 5,
        idempotency_key: str | None = None,
    ) -> EnqueuedJob:
        self.enqueued.append(payload_json)
        return EnqueuedJob(
            job_id=job_id, tenant_id=tenant_id, job_type=job_type, role=role, created=True
        )


def _claim() -> JobClaim:
    return JobClaim(
        job_id="norm-1",
        tenant_id="tenant-1",
        role=JobRole.CORE,
        job_type=NORMALIZE_JOB_TYPE,
        owner="worker-1",
        fence_token=1,
        leased_until=NOW_S,
        attempts=1,
        max_attempts=5,
        payload_json=json.dumps({"raw_revision_id": "rev-1"}),
    )


def _seed_raw_lineage(factory: sessionmaker[Session]) -> None:
    """The connection, payload, object, and revision a normalized fact points at."""
    with factory() as session, session.begin():
        session.add(
            Connection(
                id="conn-1",
                tenant_id="tenant-1",
                provider="oura",
                status="active",
                scopes_granted_json="[]",
                external_user_id=None,
                connected_at=NOW_S,
                updated_at=NOW_S,
            )
        )
        session.add(
            RawPayload(
                id="pay-1",
                tenant_id="tenant-1",
                connection_id="conn-1",
                sync_run_id=None,
                transport_kind="sync_fetch",
                provider="oura",
                stream="sleep",
                page_token=None,
                fetched_at=NOW_S,
                received_at=NOW_S,
                http_status=200,
                content_type="application/json",
                content_hash="page-hash",
                payload_json=_OURA_PAGE,
                payload_blob=None,
                request_meta_json=json.dumps({"url_template": "v2/sleep"}),
            )
        )
        session.add(
            RawObject(
                id="obj-1",
                tenant_id="tenant-1",
                connection_id="conn-1",
                provider="oura",
                stream="sleep",
                vendor_record_id="oura-sleep-1",
                current_revision_id=None,
                created_at=NOW_S,
            )
        )
        session.add(
            RawRevision(
                id="rev-1",
                tenant_id="tenant-1",
                raw_object_id="obj-1",
                raw_payload_id="pay-1",
                sync_run_id=None,
                revision_n=1,
                vendor_record_id="oura-sleep-1",
                observed_at=NOW_S,
                effective_at=NOW_S,
                received_at=NOW_S,
                content_hash="rev-hash",
                schema_version="oura.v2",
                deletion_state="active",
                is_tombstone=0,
                tombstone_reason=None,
            )
        )


def _handler(
    factory: sessionmaker[Session], selections: SourceSelectionRepository
) -> NormalizeHandler:
    facts = FactRepository(factory)
    ids = iter(f"id-{n}" for n in range(1, 500))
    return NormalizeHandler(
        revisions=_StubRevisions(),
        facts=facts,
        jobs=_StubJobs(),
        new_id=lambda: next(ids),
        sleep_providers=facts,
        selections=selections,
        clock=lambda: T0,
    )


def test_sleep_facts_by_provider_keeps_the_losers(
    factory: sessionmaker[Session],
) -> None:
    """Recording a decision needs the alternatives, not only the winner."""
    _seed_sleep(factory, provider="oura", fact_id="o", duration_min=420.0, time_in_bed_min=460.0)
    _seed_sleep(
        factory, provider="google_health", fact_id="g", duration_min=400.0, time_in_bed_min=440.0
    )

    by_provider = FactRepository(factory).sleep_facts_by_provider(
        tenant_id="tenant-1", local_health_day=DAY
    )

    # Unlike the feature queries, the losing provider survives here.
    assert by_provider == {"oura": ["o"], "google_health": ["g"]}


def test_normalize_records_the_sleep_selection(factory: sessionmaker[Session]) -> None:
    """Normalizing a night persists which provider is authoritative for it.

    A Google Health session already covers the day; the Oura revision arriving
    now wins on precedence, and the decision records the loser as a candidate.
    """
    _seed_sleep(
        factory, provider="google_health", fact_id="g", duration_min=400.0, time_in_bed_min=440.0
    )
    _seed_raw_lineage(factory)
    selections = SourceSelectionRepository(factory)

    _handler(factory, selections)(_claim())

    current = selections.current_selection(
        tenant_id="tenant-1", metric_family="sleep_session", local_health_day=DAY
    )
    assert current is not None
    assert current.selection_reason == "policy_match"
    assert current.missing_reason is None
    assert current.source_policy_version_id == SOURCE_POLICY_VERSION
    assert current.is_current == 1

    with factory() as session:
        selected_provider = session.execute(
            select(FactRecord.provider).where(FactRecord.id == current.selected_fact_record_id)
        ).scalar_one()
        candidates = session.execute(
            select(SourceSelectionCandidate.fact_record_id, SourceSelectionCandidate.rank)
            .where(SourceSelectionCandidate.source_selection_id == current.id)
            .order_by(SourceSelectionCandidate.rank)
        ).all()

    # The freshly normalized Oura fact wins over the pre-existing Google one.
    assert selected_provider == "oura"
    # Both providers are retained; the loser is an alternative, not a fallback.
    assert [fact_id for fact_id, _rank in candidates][-1] == "g"
    assert len(candidates) == 2


def test_renormalizing_does_not_stack_selection_versions(
    factory: sessionmaker[Session],
) -> None:
    """A normalize retry re-derives the same decision, so it dedupes."""
    _seed_raw_lineage(factory)
    selections = SourceSelectionRepository(factory)
    _handler(factory, selections)(_claim())
    _handler(factory, selections)(_claim())

    with factory() as session:
        versions = (
            session.execute(
                select(SourceSelection.version_n).where(SourceSelection.grain_key == DAY)
            )
            .scalars()
            .all()
        )
    assert versions == [1]


def test_single_provider_records_only_source(factory: sessionmaker[Session]) -> None:
    """Nothing competed, so the reason distinguishes it from a resolved conflict."""
    _seed_raw_lineage(factory)
    selections = SourceSelectionRepository(factory)
    _handler(factory, selections)(_claim())

    current = selections.current_selection(
        tenant_id="tenant-1", metric_family="sleep_session", local_health_day=DAY
    )
    assert current is not None
    assert current.selection_reason == "only_source"


def test_a_failing_selection_write_does_not_fail_the_job(
    factory: sessionmaker[Session],
) -> None:
    """The day's facts are already committed when the decision is recorded.

    Raising would retry a normalize whose writes all succeeded and, after enough
    attempts, dead-letter a job whose real work was done — leaving the day with
    no score recompute either. The decision is derived from stored facts, so the
    next normalize for the day re-derives it: a missing row is recoverable, a
    lost job is not.
    """

    class _BrokenSelections(SourceSelectionRepository):
        def record_daily_selection(
            self,
            *,
            selection_id: str,
            tenant_id: str,
            policy_version: str,
            spec: DailySelectionSpec,
            new_candidate_id: Callable[[], str],
            now: datetime,
        ) -> SelectionWritten:
            raise RuntimeError("selection store unavailable")

    _seed_sleep(factory, provider="oura", fact_id="o", duration_min=420.0, time_in_bed_min=460.0)
    handler = _handler(factory, _BrokenSelections(factory))

    # Must not raise: the facts are written, and the job is done.
    handler._record_selection_safely(
        handler._record_sleep_selection,
        tenant_id="tenant-1",
        day=DAY,
        now=T0,
    )
