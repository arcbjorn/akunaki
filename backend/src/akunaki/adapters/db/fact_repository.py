"""Fact persistence with versioning.

Facts are **never updated in place**. Writing a fact whose normalized content
differs from the current version supersedes that version and appends a new one,
so history stays auditable. Writing identical content is a no-op, which is what
makes re-normalization safely repeatable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.models import FactRecord, SleepSession
from akunaki.domain.jobs import require_aware, to_utc_rfc3339
from akunaki.domain.sleep_normalizer import ENTITY_TYPE, NORMALIZER_VERSION, SleepFact


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
