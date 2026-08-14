"""Fact persistence with versioning.

Facts are **never updated in place**. Writing a fact whose normalized content
differs from the current version supersedes that version and appends a new one,
so history stays auditable. Writing identical content is a no-op, which is what
makes re-normalization safely repeatable.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import InstrumentedAttribute, Session, sessionmaker

from akunaki.adapters.db.models import (
    DailyActivity,
    FactRecord,
    OvernightVitals,
    SleepSession,
    WorkoutSession,
)
from akunaki.application.workouts_surface import WorkoutSummary
from akunaki.domain.activity_normalizer import (
    ENTITY_TYPE as ACTIVITY_ENTITY_TYPE,
)
from akunaki.domain.activity_normalizer import (
    NORMALIZER_VERSION as ACTIVITY_NORMALIZER_VERSION,
)
from akunaki.domain.activity_normalizer import (
    ActivityFact,
    merged_activity_fact,
)
from akunaki.domain.connections import provider_for_schema_version
from akunaki.domain.jobs import parse_utc_rfc3339, require_aware, to_utc_rfc3339
from akunaki.domain.sleep_consistency import midpoint_local_minutes
from akunaki.domain.sleep_normalizer import ENTITY_TYPE, NORMALIZER_VERSION, SleepFact
from akunaki.domain.source_policy import authoritative_sleep_provider
from akunaki.domain.vitals_normalizer import (
    ENTITY_TYPE as VITALS_ENTITY_TYPE,
)
from akunaki.domain.vitals_normalizer import (
    NORMALIZER_VERSION as VITALS_NORMALIZER_VERSION,
)
from akunaki.domain.vitals_normalizer import (
    VitalsFact,
)
from akunaki.domain.workout_normalizer import (
    ENTITY_TYPE as WORKOUT_ENTITY_TYPE,
)
from akunaki.domain.workout_normalizer import (
    NORMALIZER_VERSION as WORKOUT_NORMALIZER_VERSION,
)
from akunaki.domain.workout_normalizer import (
    WorkoutFact,
)


@dataclass(frozen=True, slots=True)
class FactWriteOutcome:
    """What one fact write persisted."""

    fact_record_id: str
    version_n: int
    is_new_version: bool
    superseded_id: str | None = None


class FactRepository:
    """Persist versioned facts and their typed detail rows."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def write_sleep_fact(
        self,
        *,
        fact_record_id: str,
        tenant_id: str,
        connection_id: str | None,
        fact: SleepFact,
        raw_revision_id: str | None,
        raw_payload_id: str | None,
        schema_version: str,
        now: datetime,
    ) -> FactWriteOutcome:
        """Write one sleep fact, superseding any differing current version."""
        if not fact_record_id or not tenant_id:
            msg = "fact_record_id and tenant_id must be non-empty"
            raise ValueError(msg)

        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))

        with self._session_factory() as session, session.begin():
            current = session.execute(
                select(FactRecord).where(
                    # Tenant-scoped: the same vendor record id can legitimately
                    # appear for two tenants and must never collide.
                    FactRecord.tenant_id == tenant_id,
                    FactRecord.fact_key == fact.fact_key,
                    FactRecord.is_current == 1,
                )
            ).scalar_one_or_none()

            if current is not None and current.content_hash == fact.content_hash:
                # Identical normalized content: re-normalization is a no-op.
                return FactWriteOutcome(
                    fact_record_id=current.id,
                    version_n=current.version_n,
                    is_new_version=False,
                )

            next_version = 1
            superseded_id: str | None = None
            if current is not None:
                next_version = current.version_n + 1
                superseded_id = current.id
                # Retire the old version *before* inserting the new one: the
                # partial unique index permits only one current row per key.
                session.execute(
                    update(FactRecord)
                    .where(FactRecord.id == current.id)
                    .values(
                        is_current=0,
                        superseded_by=fact_record_id,
                        superseded_at=now_s,
                    )
                )
                session.flush()

            session.add(
                FactRecord(
                    id=fact_record_id,
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    provider="oura",
                    entity_type=ENTITY_TYPE,
                    vendor_record_id=fact.vendor_record_id,
                    origin=None,
                    method="wearable",
                    utc_instant=fact.start_utc,
                    start_utc=fact.start_utc,
                    end_utc=fact.end_utc,
                    source_offset_minutes=fact.source_offset_minutes,
                    iana_timezone=fact.iana_timezone,
                    local_health_day=fact.local_health_day,
                    unit=None,
                    quality=fact.quality,
                    confidence=fact.confidence,
                    freshness_at=now_s,
                    raw_revision_id=raw_revision_id,
                    raw_payload_id=raw_payload_id,
                    schema_version=schema_version,
                    normalizer_version=NORMALIZER_VERSION,
                    content_hash=fact.content_hash,
                    fact_key=fact.fact_key,
                    version_n=next_version,
                    is_current=1,
                    superseded_by=None,
                    superseded_at=None,
                    deletion_state="active",
                    exclude_from_load=0,
                    created_at=now_s,
                )
            )
            # Flush the header before its detail row: no ORM relationship
            # links them, so SQLAlchemy sorts these inserts by mapper name
            # rather than by dependency. Sleep/vitals/workout happen to sort
            # after ``FactRecord`` and activity did not — which is how the
            # activity path shipped with a latent foreign-key failure. Being
            # explicit here keeps all four independent of that accident.
            session.flush()
            session.add(
                SleepSession(
                    fact_record_id=fact_record_id,
                    tenant_id=tenant_id,
                    is_nap=1 if fact.is_nap else 0,
                    duration_min=fact.duration_min,
                    time_in_bed_min=fact.time_in_bed_min,
                    efficiency_pct=fact.efficiency_pct,
                    light_min=fact.light_min,
                    deep_min=fact.deep_min,
                    rem_min=fact.rem_min,
                    awake_min=fact.awake_min,
                )
            )

            return FactWriteOutcome(
                fact_record_id=fact_record_id,
                version_n=next_version,
                is_new_version=True,
                superseded_id=superseded_id,
            )

    def write_vitals_fact(
        self,
        *,
        fact_record_id: str,
        tenant_id: str,
        connection_id: str | None,
        fact: VitalsFact,
        raw_revision_id: str | None,
        raw_payload_id: str | None,
        schema_version: str,
        now: datetime,
    ) -> FactWriteOutcome:
        """Write one overnight-vitals fact, superseding any differing version.

        Parallels ``write_sleep_fact``: identical normalized content is a no-op,
        changed content appends a version and retires the prior current row.
        """
        if not fact_record_id or not tenant_id:
            msg = "fact_record_id and tenant_id must be non-empty"
            raise ValueError(msg)

        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))

        with self._session_factory() as session, session.begin():
            current = session.execute(
                select(FactRecord).where(
                    FactRecord.tenant_id == tenant_id,
                    FactRecord.fact_key == fact.fact_key,
                    FactRecord.is_current == 1,
                )
            ).scalar_one_or_none()

            if current is not None and current.content_hash == fact.content_hash:
                return FactWriteOutcome(
                    fact_record_id=current.id,
                    version_n=current.version_n,
                    is_new_version=False,
                )

            next_version = 1
            superseded_id: str | None = None
            if current is not None:
                next_version = current.version_n + 1
                superseded_id = current.id
                session.execute(
                    update(FactRecord)
                    .where(FactRecord.id == current.id)
                    .values(
                        is_current=0,
                        superseded_by=fact_record_id,
                        superseded_at=now_s,
                    )
                )
                session.flush()

            session.add(
                FactRecord(
                    id=fact_record_id,
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    provider="oura",
                    entity_type=VITALS_ENTITY_TYPE,
                    vendor_record_id=fact.vendor_record_id,
                    origin=None,
                    method="wearable",
                    utc_instant=fact.start_utc,
                    start_utc=fact.start_utc,
                    end_utc=fact.end_utc,
                    source_offset_minutes=fact.source_offset_minutes,
                    iana_timezone=None,
                    local_health_day=fact.local_health_day,
                    unit=None,
                    quality=fact.quality,
                    confidence=fact.confidence,
                    freshness_at=now_s,
                    raw_revision_id=raw_revision_id,
                    raw_payload_id=raw_payload_id,
                    schema_version=schema_version,
                    normalizer_version=VITALS_NORMALIZER_VERSION,
                    content_hash=fact.content_hash,
                    fact_key=fact.fact_key,
                    version_n=next_version,
                    is_current=1,
                    superseded_by=None,
                    superseded_at=None,
                    deletion_state="active",
                    exclude_from_load=0,
                    created_at=now_s,
                )
            )
            # Flush the header before its detail row: no ORM relationship
            # links them, so SQLAlchemy sorts these inserts by mapper name
            # rather than by dependency. Sleep/vitals/workout happen to sort
            # after ``FactRecord`` and activity did not — which is how the
            # activity path shipped with a latent foreign-key failure. Being
            # explicit here keeps all four independent of that accident.
            session.flush()
            session.add(
                OvernightVitals(
                    fact_record_id=fact_record_id,
                    tenant_id=tenant_id,
                    hrv_ms=fact.hrv_ms,
                    resting_hr_bpm=fact.resting_hr_bpm,
                    temperature_deviation_c=fact.temperature_deviation_c,
                    respiratory_rate_bpm=fact.respiratory_rate_bpm,
                )
            )

            return FactWriteOutcome(
                fact_record_id=fact_record_id,
                version_n=next_version,
                is_new_version=True,
                superseded_id=superseded_id,
            )

    def write_workout_fact(
        self,
        *,
        fact_record_id: str,
        tenant_id: str,
        connection_id: str | None,
        fact: WorkoutFact,
        raw_revision_id: str | None,
        raw_payload_id: str | None,
        schema_version: str,
        now: datetime,
    ) -> FactWriteOutcome:
        """Write one workout fact, superseding any differing current version."""
        if not fact_record_id or not tenant_id:
            msg = "fact_record_id and tenant_id must be non-empty"
            raise ValueError(msg)

        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))

        with self._session_factory() as session, session.begin():
            current = session.execute(
                select(FactRecord).where(
                    FactRecord.tenant_id == tenant_id,
                    FactRecord.fact_key == fact.fact_key,
                    FactRecord.is_current == 1,
                )
            ).scalar_one_or_none()

            if current is not None and current.content_hash == fact.content_hash:
                return FactWriteOutcome(
                    fact_record_id=current.id,
                    version_n=current.version_n,
                    is_new_version=False,
                )

            next_version = 1
            superseded_id: str | None = None
            if current is not None:
                next_version = current.version_n + 1
                superseded_id = current.id
                session.execute(
                    update(FactRecord)
                    .where(FactRecord.id == current.id)
                    .values(
                        is_current=0,
                        superseded_by=fact_record_id,
                        superseded_at=now_s,
                    )
                )
                session.flush()

            session.add(
                FactRecord(
                    id=fact_record_id,
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    provider="polar",
                    entity_type=WORKOUT_ENTITY_TYPE,
                    vendor_record_id=fact.vendor_record_id,
                    origin=None,
                    method="wearable",
                    utc_instant=fact.start_utc,
                    start_utc=fact.start_utc,
                    end_utc=fact.end_utc,
                    source_offset_minutes=fact.source_offset_minutes,
                    iana_timezone=None,
                    local_health_day=fact.local_health_day,
                    unit=None,
                    quality=fact.quality,
                    confidence=fact.confidence,
                    freshness_at=now_s,
                    raw_revision_id=raw_revision_id,
                    raw_payload_id=raw_payload_id,
                    schema_version=schema_version,
                    normalizer_version=WORKOUT_NORMALIZER_VERSION,
                    content_hash=fact.content_hash,
                    fact_key=fact.fact_key,
                    version_n=next_version,
                    is_current=1,
                    superseded_by=None,
                    superseded_at=None,
                    deletion_state="active",
                    exclude_from_load=0,
                    created_at=now_s,
                )
            )
            # Flush the header before its detail row: no ORM relationship
            # links them, so SQLAlchemy sorts these inserts by mapper name
            # rather than by dependency. Sleep/vitals/workout happen to sort
            # after ``FactRecord`` and activity did not — which is how the
            # activity path shipped with a latent foreign-key failure. Being
            # explicit here keeps all four independent of that accident.
            session.flush()
            session.add(
                WorkoutSession(
                    fact_record_id=fact_record_id,
                    tenant_id=tenant_id,
                    session_load=fact.session_load,
                    zone1_min=fact.zone1_min,
                    zone2_min=fact.zone2_min,
                    zone3_min=fact.zone3_min,
                    zone4_min=fact.zone4_min,
                    zone5_min=fact.zone5_min,
                )
            )

            return FactWriteOutcome(
                fact_record_id=fact_record_id,
                version_n=next_version,
                is_new_version=True,
                superseded_id=superseded_id,
            )

    def write_activity_fact(
        self,
        *,
        fact_record_id: str,
        tenant_id: str,
        connection_id: str | None,
        fact: ActivityFact,
        raw_revision_id: str | None,
        raw_payload_id: str | None,
        schema_version: str,
        now: datetime,
    ) -> FactWriteOutcome:
        """Write one daily-activity fact, superseding any differing version."""
        if not fact_record_id or not tenant_id:
            msg = "fact_record_id and tenant_id must be non-empty"
            raise ValueError(msg)

        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))
        # An unattributable fact must not be written: the column is NOT NULL, so
        # the only alternatives are a guessed provider (which would silently
        # mis-rank it in source selection) or failing here. A schema version with
        # no provider is a wiring gap in the connector, not a data condition.
        provider = provider_for_schema_version(schema_version)
        if provider is None:
            msg = f"no provider for schema version {schema_version!r}"
            raise ValueError(msg)

        with self._session_factory() as session, session.begin():
            current = session.execute(
                select(FactRecord).where(
                    FactRecord.tenant_id == tenant_id,
                    FactRecord.fact_key == fact.fact_key,
                    FactRecord.is_current == 1,
                )
            ).scalar_one_or_none()

            # A day's signals can arrive on **separate streams**: Google Health
            # reports steps and active minutes as different v4 data types, so
            # each sync writes a fact carrying one signal and a null other. Left
            # alone, the second write supersedes the first and the day ends up
            # holding whichever stream synced last, with the other reading null
            # — both measured, only one kept. Carry the absent signal forward
            # from the version being superseded so the day accumulates instead.
            #
            # Only a *null* is filled in; a present value is never overwritten by
            # an older one, so this cannot resurrect a stale reading.
            fact = _merge_activity_signals(fact, current, session)

            if current is not None and current.content_hash == fact.content_hash:
                return FactWriteOutcome(
                    fact_record_id=current.id,
                    version_n=current.version_n,
                    is_new_version=False,
                )

            next_version = 1
            superseded_id: str | None = None
            if current is not None:
                next_version = current.version_n + 1
                superseded_id = current.id
                session.execute(
                    update(FactRecord)
                    .where(FactRecord.id == current.id)
                    .values(
                        is_current=0,
                        superseded_by=fact_record_id,
                        superseded_at=now_s,
                    )
                )
                session.flush()

            session.add(
                FactRecord(
                    id=fact_record_id,
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    # Derived from the revision's schema version, never fixed to
                    # one connector: all three providers write daily activity,
                    # and the source policy groups candidates by this column, so
                    # a hardcoded provider would make every activity fact look
                    # like it came from one source and no contest could resolve.
                    provider=provider,
                    entity_type=ACTIVITY_ENTITY_TYPE,
                    vendor_record_id=fact.vendor_record_id,
                    origin=None,
                    method="wearable",
                    utc_instant=fact.start_utc,
                    start_utc=fact.start_utc,
                    end_utc=fact.end_utc,
                    source_offset_minutes=fact.source_offset_minutes,
                    iana_timezone=None,
                    local_health_day=fact.local_health_day,
                    unit=None,
                    quality=fact.quality,
                    confidence=fact.confidence,
                    freshness_at=now_s,
                    raw_revision_id=raw_revision_id,
                    raw_payload_id=raw_payload_id,
                    schema_version=schema_version,
                    normalizer_version=ACTIVITY_NORMALIZER_VERSION,
                    content_hash=fact.content_hash,
                    fact_key=fact.fact_key,
                    version_n=next_version,
                    is_current=1,
                    superseded_by=None,
                    superseded_at=None,
                    deletion_state="active",
                    exclude_from_load=0,
                    created_at=now_s,
                )
            )
            # Flush the header before its detail row: no ORM relationship links
            # them, so SQLAlchemy sorts these inserts by mapper name rather than
            # by dependency. ``DailyActivity`` sorts *before* ``FactRecord``,
            # which is how this path shipped with a latent foreign-key failure.
            session.flush()
            session.add(
                DailyActivity(
                    fact_record_id=fact_record_id,
                    tenant_id=tenant_id,
                    steps=fact.steps,
                    active_minutes=fact.active_minutes,
                )
            )

            return FactWriteOutcome(
                fact_record_id=fact_record_id,
                version_n=next_version,
                is_new_version=True,
                superseded_id=superseded_id,
            )

    def daily_activity_steps(
        self, *, tenant_id: str, local_health_days: list[str]
    ) -> dict[str, float]:
        """Daily step counts per local day where known; omit days with no steps.

        Only current, active facts count. A day with an activity fact that
        recorded active-minutes but no steps is omitted here (steps is the
        low-activity anomaly's primary series), never imputed as zero.
        """
        if not local_health_days:
            return {}
        with self._session_factory() as session:
            rows = session.execute(
                select(FactRecord.local_health_day, DailyActivity.steps)
                .join(DailyActivity, DailyActivity.fact_record_id == FactRecord.id)
                .where(
                    FactRecord.tenant_id == tenant_id,
                    FactRecord.entity_type == ACTIVITY_ENTITY_TYPE,
                    FactRecord.local_health_day.in_(local_health_days),
                    FactRecord.is_current == 1,
                    FactRecord.deletion_state == "active",
                    DailyActivity.steps.is_not(None),
                )
            ).all()
        return {day: float(steps) for day, steps in rows if day is not None and steps is not None}

    def current_sleep_facts(self, *, tenant_id: str, local_health_day: str) -> list[str]:
        """Return current sleep fact ids for a local day (newest schema first)."""
        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(FactRecord.id).where(
                        FactRecord.tenant_id == tenant_id,
                        FactRecord.entity_type == ENTITY_TYPE,
                        FactRecord.local_health_day == local_health_day,
                        FactRecord.is_current == 1,
                    )
                ).all()
            )

    def daily_sleep_durations(
        self,
        *,
        tenant_id: str,
        local_health_days: list[str],
    ) -> dict[str, float]:
        """Total sleep minutes per local day, from the authoritative provider.

        Per the design, ``sleep_duration_min`` is the total sleep minutes across
        all sessions assigned to a wake-date, naps and split sessions included —
        but **only from the one authoritative provider** for that day when more
        than one supplies sleep. Providers are never summed together or blended
        (see :func:`authoritative_sleep_provider`). Only current, load-eligible,
        active facts count; a day absent from the result has no known sleep and
        must be treated as unknown by the caller, never imputed as zero.
        """
        if not local_health_days:
            return {}
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    FactRecord.local_health_day,
                    FactRecord.provider,
                    func.sum(SleepSession.duration_min),
                )
                .join(SleepSession, SleepSession.fact_record_id == FactRecord.id)
                .where(
                    FactRecord.tenant_id == tenant_id,
                    FactRecord.entity_type == ENTITY_TYPE,
                    FactRecord.local_health_day.in_(local_health_days),
                    FactRecord.is_current == 1,
                    FactRecord.deletion_state == "active",
                    FactRecord.exclude_from_load == 0,
                )
                .group_by(FactRecord.local_health_day, FactRecord.provider)
            ).all()
        # Collapse (day, provider) totals to one authoritative provider per day.
        by_day: dict[str, dict[str, float]] = {}
        for day, provider, total in rows:
            if day is None or provider is None:
                continue
            by_day.setdefault(day, {})[provider] = float(total)
        return _authoritative_per_day(by_day)

    def daily_sleep_efficiency(
        self,
        *,
        tenant_id: str,
        local_health_days: list[str],
    ) -> dict[str, float]:
        """Sleep efficiency percent per local day: total sleep / total in-bed * 100.

        Efficiency is defined only when both totals are known, so a day is
        included only when **every** contributing session has a non-null
        ``time_in_bed_min``; a day with any missing in-bed minutes is omitted
        (absent, not imputed), matching how the baseline layer treats gaps. As
        with durations, only current, load-eligible, active facts count, and
        only the **authoritative provider's** sessions are used — providers are
        never mixed into one ratio.
        """
        if not local_health_days:
            return {}
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    FactRecord.local_health_day,
                    FactRecord.provider,
                    func.sum(SleepSession.duration_min),
                    func.sum(SleepSession.time_in_bed_min),
                    func.count().filter(SleepSession.time_in_bed_min.is_(None)),
                )
                .join(SleepSession, SleepSession.fact_record_id == FactRecord.id)
                .where(
                    FactRecord.tenant_id == tenant_id,
                    FactRecord.entity_type == ENTITY_TYPE,
                    FactRecord.local_health_day.in_(local_health_days),
                    FactRecord.is_current == 1,
                    FactRecord.deletion_state == "active",
                    FactRecord.exclude_from_load == 0,
                )
                .group_by(FactRecord.local_health_day, FactRecord.provider)
            ).all()

        # Per day, keep only the authoritative provider's totals before deciding
        # whether efficiency is defined.
        per_day: dict[str, dict[str, tuple[float, float | None, int]]] = {}
        for day, provider, duration_total, in_bed_total, missing_in_bed in rows:
            if day is None or provider is None:
                continue
            in_bed = float(in_bed_total) if in_bed_total is not None else None
            per_day.setdefault(day, {})[provider] = (
                float(duration_total),
                in_bed,
                int(missing_in_bed),
            )

        efficiency: dict[str, float] = {}
        for day, per_provider in per_day.items():
            chosen = authoritative_sleep_provider(per_provider.keys())
            if chosen is None:
                continue
            duration_total, in_bed_total, missing_in_bed = per_provider[chosen]
            if missing_in_bed > 0 or in_bed_total in (None, 0.0):
                # Any session missing in-bed minutes, or a zero/absent total,
                # leaves efficiency undefined for the day: omit it.
                continue
            assert in_bed_total is not None
            efficiency[day] = duration_total / in_bed_total * 100.0
        return efficiency

    def daily_principal_sleep_midpoint(
        self,
        *,
        tenant_id: str,
        local_health_days: list[str],
    ) -> dict[str, float]:
        """Principal-sleep local-time midpoint (minutes on [0, 1440)) per day.

        The principal session is the **non-nap** session with the longest
        duration (health-engine.md); a day with only naps has no valid night and
        is omitted. The midpoint needs the onset instant and the source offset,
        so a session missing either is skipped. Only current, load-eligible,
        active facts are considered, and only the **authoritative provider's**
        sessions — the principal session is never chosen across providers.
        """
        if not local_health_days:
            return {}
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    FactRecord.local_health_day,
                    FactRecord.provider,
                    FactRecord.start_utc,
                    FactRecord.source_offset_minutes,
                    SleepSession.duration_min,
                )
                .join(SleepSession, SleepSession.fact_record_id == FactRecord.id)
                .where(
                    FactRecord.tenant_id == tenant_id,
                    FactRecord.entity_type == ENTITY_TYPE,
                    FactRecord.local_health_day.in_(local_health_days),
                    FactRecord.is_current == 1,
                    FactRecord.deletion_state == "active",
                    FactRecord.exclude_from_load == 0,
                    SleepSession.is_nap == 0,
                )
            ).all()

        # The authoritative provider per day is decided over every provider that
        # supplied a non-nap session, so a lower-precedence provider's session is
        # never the principal one even if it is longer.
        providers_by_day: dict[str, set[str]] = {}
        for day, provider, _start, _offset, _dur in rows:
            if day is not None and provider is not None:
                providers_by_day.setdefault(day, set()).add(provider)
        authoritative = {
            day: authoritative_sleep_provider(providers)
            for day, providers in providers_by_day.items()
        }

        # Pick the longest non-nap authoritative session per day, then midpoint.
        principal_duration: dict[str, float] = {}
        midpoints: dict[str, float] = {}
        for day, provider, start_utc, offset_minutes, duration_min in rows:
            if day is None or start_utc is None or offset_minutes is None:
                continue
            if provider != authoritative.get(day):
                continue
            if duration_min <= principal_duration.get(day, -1.0):
                continue
            start_local = parse_utc_rfc3339(start_utc) + timedelta(minutes=offset_minutes)
            start_local_minutes = (
                start_local.hour * 60 + start_local.minute + start_local.second / 60.0
            )
            principal_duration[day] = float(duration_min)
            midpoints[day] = midpoint_local_minutes(
                start_local_minutes=start_local_minutes,
                duration_minutes=float(duration_min),
            )
        return midpoints

    def daily_hrv(
        self,
        *,
        tenant_id: str,
        local_health_days: list[str],
    ) -> dict[str, float]:
        """Overnight HRV (ms) per local day; omit days with no HRV reading."""
        return self._daily_vital(
            tenant_id=tenant_id,
            local_health_days=local_health_days,
            column=OvernightVitals.hrv_ms,
        )

    def daily_resting_hr(
        self,
        *,
        tenant_id: str,
        local_health_days: list[str],
    ) -> dict[str, float]:
        """Overnight resting HR (bpm) per local day; omit days with none."""
        return self._daily_vital(
            tenant_id=tenant_id,
            local_health_days=local_health_days,
            column=OvernightVitals.resting_hr_bpm,
        )

    def daily_temperature_deviation(
        self,
        *,
        tenant_id: str,
        local_health_days: list[str],
    ) -> dict[str, float]:
        """Overnight temperature deviation (°C) per local day; omit days with none."""
        return self._daily_vital(
            tenant_id=tenant_id,
            local_health_days=local_health_days,
            column=OvernightVitals.temperature_deviation_c,
        )

    def daily_respiratory_rate(
        self,
        *,
        tenant_id: str,
        local_health_days: list[str],
    ) -> dict[str, float]:
        """Overnight respiration rate (breaths/min) per local day; omit days with none."""
        return self._daily_vital(
            tenant_id=tenant_id,
            local_health_days=local_health_days,
            column=OvernightVitals.respiratory_rate_bpm,
        )

    def list_workouts(
        self,
        *,
        tenant_id: str,
        limit: int,
        cursor: str | None = None,
    ) -> tuple[list[WorkoutSummary], str | None]:
        """One page of a tenant's workouts, newest first, plus the next cursor.

        Keyset pagination on ``(start_utc, fact_record_id)`` rather than an
        offset: workouts arrive continuously, and an offset would skip or repeat
        rows once a new sync lands between pages. The id breaks ties so two
        sessions starting in the same second still order deterministically.

        Excludes ``exclude_from_load = 1`` rows. That flag marks a *duplicate*
        of the same real session from a second provider, so including it here
        would show the user one workout twice.
        """
        conditions = [
            FactRecord.tenant_id == tenant_id,
            FactRecord.entity_type == WORKOUT_ENTITY_TYPE,
            FactRecord.is_current == 1,
            FactRecord.deletion_state == "active",
            FactRecord.exclude_from_load == 0,
        ]
        if cursor is not None:
            decoded = _decode_workout_cursor(cursor)
            if decoded is None:
                # A malformed cursor is a client error, not a silent full scan.
                msg = "cursor is not a valid workout cursor"
                raise ValueError(msg)
            last_start, last_id = decoded
            # Strictly "older than" the last row of the previous page.
            conditions.append(
                or_(
                    FactRecord.start_utc < last_start,
                    and_(FactRecord.start_utc == last_start, FactRecord.id < last_id),
                )
            )

        with self._session_factory() as session:
            rows = session.execute(
                select(FactRecord, WorkoutSession)
                .join(WorkoutSession, WorkoutSession.fact_record_id == FactRecord.id)
                .where(*conditions)
                .order_by(FactRecord.start_utc.desc(), FactRecord.id.desc())
                # One extra row reveals whether another page exists without a
                # second count query.
                .limit(limit + 1)
            ).all()

        has_more = len(rows) > limit
        page = [_workout_summary(fact, detail) for fact, detail in rows[:limit]]
        next_cursor = None
        if has_more and page:
            last = rows[limit - 1][0]
            next_cursor = _encode_workout_cursor(last.start_utc, last.id)
        return page, next_cursor

    def get_workout(self, *, tenant_id: str, workout_id: str) -> WorkoutSummary | None:
        """One workout the tenant owns, or None when unknown or not theirs.

        Tenant-scoped in the query itself, so another tenant's id resolves to
        None exactly like a nonexistent one.
        """
        with self._session_factory() as session:
            row = session.execute(
                select(FactRecord, WorkoutSession)
                .join(WorkoutSession, WorkoutSession.fact_record_id == FactRecord.id)
                .where(
                    FactRecord.id == workout_id,
                    FactRecord.tenant_id == tenant_id,
                    FactRecord.entity_type == WORKOUT_ENTITY_TYPE,
                    FactRecord.is_current == 1,
                    FactRecord.deletion_state == "active",
                )
            ).first()
        if row is None:
            return None
        return _workout_summary(row[0], row[1])

    def daily_strain_load(
        self,
        *,
        tenant_id: str,
        local_health_days: list[str],
    ) -> dict[str, float]:
        """Daily strain-load per local day: the sum of included session loads.

        Only current, load-eligible (``exclude_from_load = 0``), active workout
        facts count. A day with **no** workout fact is absent from the result —
        the caller treats it as unknown coverage, never a zero. (A confirmed
        rest day with coverage would be a stored zero-load workout fact, which
        sums to 0 here.)
        """
        if not local_health_days:
            return {}
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    FactRecord.local_health_day,
                    func.sum(WorkoutSession.session_load),
                )
                .join(WorkoutSession, WorkoutSession.fact_record_id == FactRecord.id)
                .where(
                    FactRecord.tenant_id == tenant_id,
                    FactRecord.entity_type == WORKOUT_ENTITY_TYPE,
                    FactRecord.local_health_day.in_(local_health_days),
                    FactRecord.is_current == 1,
                    FactRecord.deletion_state == "active",
                    FactRecord.exclude_from_load == 0,
                )
                .group_by(FactRecord.local_health_day)
            ).all()
        return {day: float(total) for day, total in rows if day is not None}

    def _daily_vital(
        self,
        *,
        tenant_id: str,
        local_health_days: list[str],
        column: InstrumentedAttribute[float | None],
    ) -> dict[str, float]:
        """One overnight-vitals scalar per local day, skipping null readings.

        There is one main-sleep vitals fact per wake-date, so no aggregation is
        needed; a day whose current fact has a null value for the requested
        metric is omitted (absent, not imputed).
        """
        if not local_health_days:
            return {}
        with self._session_factory() as session:
            rows = session.execute(
                select(FactRecord.local_health_day, column)
                .join(OvernightVitals, OvernightVitals.fact_record_id == FactRecord.id)
                .where(
                    FactRecord.tenant_id == tenant_id,
                    FactRecord.entity_type == VITALS_ENTITY_TYPE,
                    FactRecord.local_health_day.in_(local_health_days),
                    FactRecord.is_current == 1,
                    FactRecord.deletion_state == "active",
                    FactRecord.exclude_from_load == 0,
                    column.is_not(None),
                )
            ).all()
        return {day: float(value) for day, value in rows if day is not None and value is not None}

    def fact_ids_for_day(self, *, tenant_id: str, local_health_day: str) -> dict[str, list[str]]:
        """Current fact-record ids on one local day, grouped by entity type.

        Provenance needs to name the facts a score was derived *from*, not just
        the roles it used. Only the target day is returned: it is the day the
        score is attributed to, and the 42-day baseline window behind it is
        context that the formula version plus these facts already determine.

        The filters mirror the feature queries exactly (current version, active,
        load-eligible), so a fact recorded as an input is the same row the
        component actually read. Sleep collapses to the **authoritative
        provider** for the day, matching how the sleep features select — a
        candidate that lost source selection never reads as an input.
        """
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    FactRecord.id,
                    FactRecord.entity_type,
                    FactRecord.provider,
                )
                .where(
                    FactRecord.tenant_id == tenant_id,
                    FactRecord.local_health_day == local_health_day,
                    FactRecord.is_current == 1,
                    FactRecord.deletion_state == "active",
                    FactRecord.exclude_from_load == 0,
                )
                .order_by(FactRecord.id)
            ).all()

        by_entity: dict[str, list[str]] = {}
        sleep_by_provider: dict[str, list[str]] = {}
        for fact_id, entity_type, provider in rows:
            if entity_type == ENTITY_TYPE:
                # Defer: only the authoritative provider's sessions are inputs.
                if provider is not None:
                    sleep_by_provider.setdefault(provider, []).append(fact_id)
                continue
            by_entity.setdefault(entity_type, []).append(fact_id)

        chosen = authoritative_sleep_provider(sleep_by_provider.keys())
        if chosen is not None:
            by_entity[ENTITY_TYPE] = sleep_by_provider[chosen]
        return by_entity

    def sleep_facts_by_provider(
        self, *, tenant_id: str, local_health_day: str
    ) -> dict[str, list[str]]:
        """Current sleep fact ids on one local day, grouped by provider.

        Unlike :meth:`fact_ids_for_day`, this keeps the **losing** providers:
        recording a source selection needs the alternatives (the "Why"), not
        just the winner. Ordered by id so a re-recorded decision is byte-stable
        and dedupes instead of writing a new version.
        """
        with self._session_factory() as session:
            rows = session.execute(
                select(FactRecord.provider, FactRecord.id)
                .where(
                    FactRecord.tenant_id == tenant_id,
                    FactRecord.entity_type == ENTITY_TYPE,
                    FactRecord.local_health_day == local_health_day,
                    FactRecord.is_current == 1,
                    FactRecord.deletion_state == "active",
                    FactRecord.exclude_from_load == 0,
                )
                .order_by(FactRecord.id)
            ).all()

        by_provider: dict[str, list[str]] = {}
        for provider, fact_id in rows:
            if provider is not None:
                by_provider.setdefault(provider, []).append(fact_id)
        return by_provider


def _workout_summary(fact: FactRecord, detail: WorkoutSession) -> WorkoutSummary:
    """Project a fact + its workout detail into the disclosed summary."""
    return WorkoutSummary(
        workout_id=fact.id,
        provider=fact.provider or "",
        local_health_day=fact.local_health_day or "",
        start_utc=fact.start_utc or "",
        end_utc=fact.end_utc or "",
        session_load=float(detail.session_load),
        zone1_min=float(detail.zone1_min),
        zone2_min=float(detail.zone2_min),
        zone3_min=float(detail.zone3_min),
        zone4_min=float(detail.zone4_min),
        zone5_min=float(detail.zone5_min),
    )


def _encode_workout_cursor(start_utc: str, fact_id: str) -> str:
    """Opaque cursor for the last row of a page.

    Base64 of ``start_utc|id``. Opaque so clients treat it as a handle rather
    than a queryable filter, and so the keyset columns can change without
    breaking a client that stored one.
    """
    raw = f"{start_utc}|{fact_id}".encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_workout_cursor(cursor: str) -> tuple[str, str] | None:
    """Decode a cursor to ``(start_utc, fact_id)``, or None when malformed."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    start_utc, separator, fact_id = raw.partition("|")
    if not separator or not start_utc or not fact_id:
        return None
    return start_utc, fact_id


def _authoritative_per_day(by_day: dict[str, dict[str, float]]) -> dict[str, float]:
    """Collapse per-provider day values to the one authoritative provider's.

    ``by_day`` maps each day to ``{provider: value}``. For each day the sleep
    precedence picks a single provider; a day whose only providers are outside
    the precedence contributes nothing (no authoritative source), rather than
    blending unrecognized sources.
    """
    result: dict[str, float] = {}
    for day, per_provider in by_day.items():
        chosen = authoritative_sleep_provider(per_provider.keys())
        if chosen is not None:
            result[day] = per_provider[chosen]
    return result


def _merge_activity_signals(
    fact: ActivityFact,
    current: FactRecord | None,
    session: Session,
) -> ActivityFact:
    """Fill a fact's absent activity signals from the version it supersedes.

    Returns ``fact`` unchanged when there is no prior version or nothing to
    carry forward, so a first write and a full-signal write are untouched.
    """
    if current is None:
        return fact
    if fact.steps is not None and fact.active_minutes is not None:
        return fact
    detail = session.get(DailyActivity, current.id)
    if detail is None:
        return fact
    return merged_activity_fact(
        fact,
        steps=detail.steps,
        active_minutes=detail.active_minutes,
    )
