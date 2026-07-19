"""Oura sleep normalizer: vendor payload to canonical sleep facts.

Pure: no I/O, no clock. Every timestamp comes from the payload, never
``now()`` — a re-run over the same raw revision must produce byte-identical
facts, which is what makes normalization safely repeatable.

Canonical rules applied here:

- **Wake-date assignment.** A sleep bout belongs to the local date of *wake*,
  not onset, so a 23:00→07:00 night counts for the morning it ended.
- **Canonical units.** Durations are stored in minutes; Oura reports seconds.
- **Quality** degrades when stage detail is missing, rather than silently
  presenting a partial night as a complete one.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from akunaki.domain.jobs import parse_utc_rfc3339, to_utc_rfc3339

NORMALIZER_VERSION = "oura_sleep_v0.1.0"
ENTITY_TYPE = "sleep_session"

# Oura reports a short nap as a distinct sleep type; anything under this is
# treated as a nap rather than a main sleep bout.
NAP_MAX_MINUTES = 180.0


class NormalizationError(Exception):
    """Payload could not be normalized. Carries no vendor body."""


@dataclass(frozen=True, slots=True)
class SleepFact:
    """One canonical sleep session ready to persist."""

    vendor_record_id: str
    start_utc: str
    end_utc: str
    local_health_day: str
    iana_timezone: str | None
    source_offset_minutes: int | None
    duration_min: float
    time_in_bed_min: float | None
    efficiency_pct: float | None
    light_min: float | None
    deep_min: float | None
    rem_min: float | None
    awake_min: float | None
    is_nap: bool
    quality: str
    confidence: float
    content_hash: str

    @property
    def fact_key(self) -> str:
        """Stable logical identity across versions of this session."""
        return f"{ENTITY_TYPE}:{self.vendor_record_id}"


def normalize_sleep_payload(payload_text: str) -> list[SleepFact]:
    """Normalize an Oura V2 sleep collection page into canonical facts.

    Raises :class:`NormalizationError` for a structurally unusable payload;
    individual records that are unusable are skipped rather than failing the
    whole page, so one bad record cannot block a night's ingestion.
    """
    try:
        parsed = json.loads(payload_text)
    except ValueError as exc:
        msg = "payload is not valid json"
        raise NormalizationError(msg) from exc

    if not isinstance(parsed, dict):
        msg = "payload root must be an object"
        raise NormalizationError(msg)

    records = parsed.get("data")
    if not isinstance(records, list):
        msg = "payload has no data array"
        raise NormalizationError(msg)

    facts: list[SleepFact] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        fact = _normalize_record(record)
        if fact is not None:
            facts.append(fact)
    return facts


def _normalize_record(record: dict[str, Any]) -> SleepFact | None:
    """Normalize one Oura sleep record, or None when unusable."""
    vendor_id = record.get("id")
    bedtime_start = record.get("bedtime_start")
    bedtime_end = record.get("bedtime_end")
    if not isinstance(vendor_id, str) or not vendor_id:
        return None
    if not isinstance(bedtime_start, str) or not isinstance(bedtime_end, str):
        return None

    try:
        start = parse_utc_rfc3339(bedtime_start)
        end = parse_utc_rfc3339(bedtime_end)
    except ValueError:
        return None
    if end <= start:
        return None

    offset_minutes = _offset_minutes(bedtime_end)
    local_day = _wake_local_date(end, offset_minutes)

    total_sleep_min = _seconds_to_minutes(record.get("total_sleep_duration"))
    time_in_bed_min = _seconds_to_minutes(record.get("time_in_bed"))
    duration_min = total_sleep_min if total_sleep_min is not None else _span_minutes(start, end)

    light = _seconds_to_minutes(record.get("light_sleep_duration"))
    deep = _seconds_to_minutes(record.get("deep_sleep_duration"))
    rem = _seconds_to_minutes(record.get("rem_sleep_duration"))
    awake = _seconds_to_minutes(record.get("awake_time"))

    efficiency = record.get("efficiency")
    efficiency_pct = float(efficiency) if isinstance(efficiency, int | float) else None
    if efficiency_pct is not None and not 0.0 <= efficiency_pct <= 100.0:
        efficiency_pct = None

    has_stages = any(value is not None for value in (light, deep, rem))
    quality, confidence = _quality_for(
        has_stages=has_stages,
        has_total=total_sleep_min is not None,
    )

    sleep_type = record.get("type")
    is_nap = sleep_type == "nap" if isinstance(sleep_type, str) else duration_min <= NAP_MAX_MINUTES

    fact = SleepFact(
        vendor_record_id=vendor_id,
        start_utc=to_utc_rfc3339(start),
        end_utc=to_utc_rfc3339(end),
        local_health_day=local_day,
        iana_timezone=None,
        source_offset_minutes=offset_minutes,
        duration_min=duration_min,
        time_in_bed_min=time_in_bed_min,
        efficiency_pct=efficiency_pct,
        light_min=light,
        deep_min=deep,
        rem_min=rem,
        awake_min=awake,
        is_nap=is_nap,
        quality=quality,
        confidence=confidence,
        content_hash="",
    )
    return _with_content_hash(fact)


def _with_content_hash(fact: SleepFact) -> SleepFact:
    """Attach a hash over the normalized values, for change detection.

    Computed from canonical values only, so a re-run that produces the same
    facts yields the same hash and therefore writes no new version.
    """
    material = json.dumps(
        {
            "vendor_record_id": fact.vendor_record_id,
            "start_utc": fact.start_utc,
            "end_utc": fact.end_utc,
            "local_health_day": fact.local_health_day,
            "duration_min": fact.duration_min,
            "time_in_bed_min": fact.time_in_bed_min,
            "efficiency_pct": fact.efficiency_pct,
            "light_min": fact.light_min,
            "deep_min": fact.deep_min,
            "rem_min": fact.rem_min,
            "awake_min": fact.awake_min,
            "is_nap": fact.is_nap,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return SleepFact(
        vendor_record_id=fact.vendor_record_id,
        start_utc=fact.start_utc,
        end_utc=fact.end_utc,
        local_health_day=fact.local_health_day,
        iana_timezone=fact.iana_timezone,
        source_offset_minutes=fact.source_offset_minutes,
        duration_min=fact.duration_min,
        time_in_bed_min=fact.time_in_bed_min,
        efficiency_pct=fact.efficiency_pct,
        light_min=fact.light_min,
        deep_min=fact.deep_min,
        rem_min=fact.rem_min,
        awake_min=fact.awake_min,
        is_nap=fact.is_nap,
        quality=fact.quality,
        confidence=fact.confidence,
        content_hash=digest,
    )


def _wake_local_date(end_utc: datetime, offset_minutes: int | None) -> str:
    """Return the local date of **wake**, per the canonical assignment rule."""
    local = end_utc + timedelta(minutes=offset_minutes or 0)
    return local.date().isoformat()


def _offset_minutes(timestamp_text: str) -> int | None:
    """Extract the UTC offset the vendor reported, in minutes."""
    try:
        parsed = datetime.fromisoformat(timestamp_text)
    except ValueError:
        return None
    offset = parsed.utcoffset()
    if offset is None:
        return None
    return int(offset.total_seconds() // 60)


def _seconds_to_minutes(value: object) -> float | None:
    """Convert vendor seconds to canonical minutes."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    if value < 0:
        return None
    return round(float(value) / 60.0, 3)


def _span_minutes(start: datetime, end: datetime) -> float:
    return round((end - start).total_seconds() / 60.0, 3)


def _quality_for(*, has_stages: bool, has_total: bool) -> tuple[str, float]:
    """Grade a record: missing stage detail lowers quality, never hides it."""
    if has_stages and has_total:
        return "high", 0.95
    if has_total:
        return "medium", 0.7
    # Duration inferred from the bed interval alone.
    return "low", 0.4
