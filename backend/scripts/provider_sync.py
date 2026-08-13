"""Run a real sync end to end against a linked connection (local dev).

Drives the production worker path — the same `build_registry` wiring the real
worker entrypoint uses — so this exercises fetch -> ingest -> normalize ->
score, not a stub. Jobs are enqueued and drained through the real `JobWorker`,
so retries, leases, and dead-lettering behave exactly as in production.

Usage:

    uv run python scripts/provider_sync.py --provider oura
    uv run python scripts/provider_sync.py --provider polar --report
    uv run python scripts/provider_sync.py --all --report
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.job_repository import JobRepository
from akunaki.adapters.db.models import (
    Connection,
    DailyActivity,
    FactRecord,
    OvernightVitals,
    RawRevision,
    SleepSession,
    WorkoutSession,
)
from akunaki.adapters.wiring.registry import build_registry
from akunaki.application.sync_handlers import streams_for_provider
from akunaki.application.worker_runtime import JobWorker, WorkerConfig
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import INITIAL_SYNC_JOB_TYPE

PROVIDERS = ("oura", "polar", "google_health")
MAX_DRAIN = 200


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--provider", choices=PROVIDERS)
    target.add_argument("--all", action="store_true", help="sync every linked provider")
    parser.add_argument("--report", action="store_true", help="print what landed")
    args = parser.parse_args()

    clear_settings_cache()
    settings = Settings()
    engine = create_db_engine(settings)
    factory = create_session_factory(engine)

    with factory() as session:
        query = select(Connection)
        if not args.all:
            query = query.where(Connection.provider == args.provider)
        connections = [(c.id, c.tenant_id, c.provider) for c in session.scalars(query)]

    if not connections:
        which = "any provider" if args.all else args.provider
        raise SystemExit(
            f"no connection linked for {which} — run scripts/provider_link.py link first"
        )

    now = datetime.now(UTC)
    jobs = JobRepository(factory)
    for connection_id, tenant_id, provider in connections:
        # One job per stream, mirroring the reconcile sweep's fan-out: a
        # stream-less job would run only the provider's primary stream.
        for stream, _schema in streams_for_provider(provider):
            print(f"enqueuing initial sync: provider={provider} stream={stream}")
            jobs.enqueue_job(
                job_id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                job_type=INITIAL_SYNC_JOB_TYPE,
                payload_json=json.dumps({"connection_id": connection_id, "stream": stream}),
                now=now,
                max_attempts=3,
            )

    worker = JobWorker(
        jobs,
        owner="local-sync",
        config=WorkerConfig(lease_ttl=timedelta(seconds=120)),
        registry=build_registry(settings, factory),
        jitter=lambda: 0.0,
    )

    # Sync enqueues normalize, which enqueues score.recompute; drain until idle
    # so the whole chain runs in one pass.
    drained = 0
    while drained < MAX_DRAIN and worker.run_once():
        drained += 1

    stats = worker.stats
    print(
        f"\njobs run: {drained} | succeeded={stats.succeeded} "
        f"retried={stats.retried} dead_lettered={stats.dead_lettered}"
    )
    if stats.dead_lettered:
        print("  a dead-lettered job means the chain stopped; check job_dead_letters.")

    if args.report:
        _report(factory)


def _report(factory: sessionmaker[Session]) -> None:
    # The day/quality live on FactRecord; the detail tables hold the values.
    with factory() as session:
        revisions = session.scalar(select(func.count()).select_from(RawRevision)) or 0
        sleep_rows = session.execute(
            select(FactRecord, SleepSession)
            .join(SleepSession, SleepSession.fact_record_id == FactRecord.id)
            .where(FactRecord.is_current.is_(True))
        ).all()
        vitals_rows = session.execute(
            select(FactRecord, OvernightVitals)
            .join(OvernightVitals, OvernightVitals.fact_record_id == FactRecord.id)
            .where(FactRecord.is_current.is_(True))
        ).all()
        workout_count = (
            session.scalar(
                select(func.count())
                .select_from(WorkoutSession)
                .join(FactRecord, FactRecord.id == WorkoutSession.fact_record_id)
                .where(FactRecord.is_current.is_(True))
            )
            or 0
        )
        activity_count = (
            session.scalar(
                select(func.count())
                .select_from(DailyActivity)
                .join(FactRecord, FactRecord.id == DailyActivity.fact_record_id)
                .where(FactRecord.is_current.is_(True))
            )
            or 0
        )

    naps = sum(1 for _fact, sleep in sleep_rows if sleep.is_nap)
    non_nap = len(sleep_rows) - naps

    print(f"raw revisions   : {revisions}")
    print(f"sleep sessions  : {len(sleep_rows)}  (naps: {naps}, main: {non_nap})")
    print(f"overnight vitals: {len(vitals_rows)}")
    print(f"workouts        : {workout_count}")
    print(f"daily activity  : {activity_count}")
    if len(vitals_rows) > non_nap:
        print("  WARNING: more vitals than main sleeps — the nap guard has regressed.")
    else:
        print("  ok: vitals never exceed main sleeps, so no nap leaked in.")

    for fact, vitals in sorted(vitals_rows, key=lambda r: r[0].local_health_day)[:7]:
        print(
            f"  {fact.local_health_day}  hrv={vitals.hrv_ms}  rhr={vitals.resting_hr_bpm}  "
            f"temp={vitals.temperature_deviation_c}  resp={vitals.respiratory_rate_bpm}"
        )


if __name__ == "__main__":
    main()
