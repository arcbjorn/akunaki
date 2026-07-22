"""Polar workout normalizer: exercise payload to canonical workout facts.

Pure: no I/O, no clock. Every timestamp comes from the payload, never
``now()`` — a re-run over the same raw revision produces byte-identical facts.

Polar's exercise records carry per-HR-zone durations; the canonical training
load is computed **internally** from those via :func:`session_load`, never taken
from any vendor-provided load field. The fact is assigned to the local date of
the exercise, sanity-checked, and its zone minutes retained so the load can be
recomputed under a new zone-weight/formula version.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from akunaki.domain.jobs import parse_utc_rfc3339, to_utc_rfc3339
from akunaki.domain.zone_load import ZoneMinutes, session_load

NORMALIZER_VERSION = "polar_workout_v0.1.0"
ENTITY_TYPE = "workout_session"

# Cap on a single zone's minutes; a longer value is a payload error, dropped.
_MAX_ZONE_MINUTES = 24.0 * 60.0


class NormalizationError(Exception):
    """Payload could not be normalized. Carries no vendor body."""


@dataclass(frozen=True, slots=True)
class WorkoutFact:
    """One canonical workout with internally computed zone-load."""

    vendor_record_id: str
    start_utc: str
    end_utc: str
    local_health_day: str
    source_offset_minutes: int | None
    session_load: float
    zone1_min: float
    zone2_min: float
    zone3_min: float
    zone4_min: float
    zone5_min: float
    quality: str
    confidence: float
    content_hash: str

    @property
    def fact_key(self) -> str:
        """Stable logical identity across versions of this workout."""
        return f"{ENTITY_TYPE}:{self.vendor_record_id}"


def normalize_workout_payload(payload_text: str) -> list[WorkoutFact]:
    """Normalize a Polar exercise page into canonical workout facts.

    Raises :class:`NormalizationError` for a structurally unusable payload;
    individual records without usable zone data are skipped rather than failing
    the page.
    """
    try:
        parsed = json.loads(payload_text)
    except ValueError as exc:
        msg = "payload is not valid json"
        raise NormalizationError(msg) from exc

    if isinstance(parsed, list):
        records = parsed
    elif isinstance(parsed, dict) and isinstance(parsed.get("data"), list):
        records = parsed["data"]
    elif isinstance(parsed, dict) and ("start_time" in parsed or "start-time" in parsed):
        records = [parsed]
    else:
        msg = "payload has no exercise records"
        raise NormalizationError(msg)

    facts: list[WorkoutFact] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        fact = _normalize_record(record)
        if fact is not None:
            facts.append(fact)
    return facts


def _normalize_record(record: dict[str, Any]) -> WorkoutFact | None:
    """Normalize one Polar exercise record, or None when unusable."""
    vendor_id = record.get("id")
    start_text = record.get("start_time") or record.get("start-time")
    if not isinstance(vendor_id, str) or not vendor_id:
        return None
    if not isinstance(start_text, str):
        return None

    try:
        start = parse_utc_rfc3339(start_text)
    except ValueError:
        return None

    zones = _zone_minutes(record.get("heart_rate_zones") or record.get("zones"))
    if zones is None:
        return None
    load = session_load(zones)

    duration_min = _duration_minutes(record.get("duration"))
    end = start + timedelta(minutes=duration_min) if duration_min is not None else start
    offset_minutes = _offset_minutes(start_text)
    local_day = _local_date(start, offset_minutes)

    fact = WorkoutFact(
        vendor_record_id=vendor_id,
        start_utc=to_utc_rfc3339(start),
        end_utc=to_utc_rfc3339(end),
        local_health_day=local_day,
        source_offset_minutes=offset_minutes,
        session_load=load,
        zone1_min=zones.z1,
        zone2_min=zones.z2,
        zone3_min=zones.z3,
        zone4_min=zones.z4,
        zone5_min=zones.z5,
        quality="high",
        confidence=0.9,
        content_hash="",
    )
    return _with_content_hash(fact)


def _zone_minutes(raw: object) -> ZoneMinutes | None:
    """Parse the five HR-zone durations (minutes), or None when incomplete.

    Accepts a list of five zone objects each with an ``in_zone`` duration, or a
    mapping ``{"zone1": min, ...}``. All five must be present and in range.
    """
    values: list[float] = []
    if isinstance(raw, list) and len(raw) == 5:
        for entry in raw:
            minutes = _duration_minutes(entry.get("in_zone") if isinstance(entry, dict) else None)
            if minutes is None:
                return None
            values.append(minutes)
    elif isinstance(raw, dict):
        for key in ("zone1", "zone2", "zone3", "zone4", "zone5"):
            minutes = _duration_minutes(raw.get(key))
            if minutes is None:
                return None
            values.append(minutes)
    else:
        return None

    if any(not 0.0 <= v <= _MAX_ZONE_MINUTES for v in values):
        return None
    return ZoneMinutes(z1=values[0], z2=values[1], z3=values[2], z4=values[3], z5=values[4])


def _duration_minutes(value: object) -> float | None:
    """Parse an ISO-8601 duration (``PT1H30M``) or a numeric seconds value to minutes."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        if value < 0:
            return None
        return round(float(value) / 60.0, 3)
    if isinstance(value, str):
        seconds = _parse_iso8601_duration(value)
        return round(seconds / 60.0, 3) if seconds is not None else None
    return None


def _parse_iso8601_duration(text: str) -> float | None:
    """Parse a subset of ISO-8601 durations (hours/minutes/seconds) to seconds."""
    if not text.startswith("PT"):
        return None
    total = 0.0
    number = ""
    for char in text[2:]:
        if char.isdigit() or char == ".":
            number += char
            continue
        if not number:
            return None
        value = float(number)
        number = ""
        if char == "H":
            total += value * 3600.0
        elif char == "M":
            total += value * 60.0
        elif char == "S":
            total += value
        else:
            return None
    return total if not number else None


def _local_date(start_utc: datetime, offset_minutes: int | None) -> str:
    """Local calendar date of the exercise start."""
    local = start_utc + timedelta(minutes=offset_minutes or 0)
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


def _with_content_hash(fact: WorkoutFact) -> WorkoutFact:
    """Attach a hash over the normalized values, for change detection."""
    material = json.dumps(
        {
            "vendor_record_id": fact.vendor_record_id,
            "start_utc": fact.start_utc,
            "end_utc": fact.end_utc,
            "local_health_day": fact.local_health_day,
            "session_load": fact.session_load,
            "zones": [
                fact.zone1_min,
                fact.zone2_min,
                fact.zone3_min,
                fact.zone4_min,
                fact.zone5_min,
            ],
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return WorkoutFact(
        vendor_record_id=fact.vendor_record_id,
        start_utc=fact.start_utc,
        end_utc=fact.end_utc,
        local_health_day=fact.local_health_day,
        source_offset_minutes=fact.source_offset_minutes,
        session_load=fact.session_load,
        zone1_min=fact.zone1_min,
        zone2_min=fact.zone2_min,
        zone3_min=fact.zone3_min,
        zone4_min=fact.zone4_min,
        zone5_min=fact.zone5_min,
        quality=fact.quality,
        confidence=fact.confidence,
        content_hash=digest,
    )
