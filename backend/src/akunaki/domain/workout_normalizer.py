"""Polar workout normalizer: exercise payload to canonical workout facts.

Pure: no I/O, no clock. Every timestamp comes from the payload, never
``now()`` — a re-run over the same raw revision produces byte-identical facts.

Reads the ``exerciseHashId`` shape that ``GET /v3/exercises?zones=true``
returns: snake_case fields, HR-zone durations inlined under
``heart_rate_zones``, and — the one trap — a ``start_time`` that is **local
wall-clock time carrying no zone**, whose offset arrives separately in
``start_time_utc_offset``. The two are combined into a real instant; a record
missing the offset is dropped rather than pinned to a guessed timezone.

The canonical training load is computed **internally** from the zone durations
via :func:`session_load`, never taken from the vendor's own ``training_load``
field. The fact is assigned to the local date of the exercise, sanity-checked,
and its zone minutes retained so the load can be recomputed under a new
zone-weight/formula version.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from akunaki.domain.jobs import to_utc_rfc3339
from akunaki.domain.zone_load import ZoneMinutes, session_load

NORMALIZER_VERSION = "polar_workout_v0.1.0"
ENTITY_TYPE = "workout_session"

# Cap on a single zone's minutes; a longer value is a payload error, dropped.
_MAX_ZONE_MINUTES = 24.0 * 60.0

# Bound on a plausible UTC offset, in minutes (±24h).
_MAX_OFFSET_MINUTES = 24 * 60


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

    # Two shapes reach this normalizer, both produced in-tree:
    #   * one bare exercise object — the per-record slice `split_page` stores as
    #     a revision, which is what the normalize handler actually passes;
    #   * the whole array `GET /v3/exercises` returns, used when normalizing a
    #     page directly.
    # Anything else is an upstream bug rather than a payload to guess at.
    if isinstance(parsed, list):
        records: list[Any] = parsed
    elif isinstance(parsed, dict) and "start_time" in parsed:
        records = [parsed]
    else:
        msg = "payload must be an exercise object or an exercises array"
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
    start_text = record.get("start_time")
    if not isinstance(vendor_id, str) or not vendor_id:
        return None
    if not isinstance(start_text, str):
        return None

    # AccessLink reports `start_time` as **local wall-clock time with no zone**
    # and carries the zone separately in `start_time_utc_offset` (minutes). The
    # two must be combined to get a real instant; parsing the timestamp alone
    # would yield a naive datetime, which the RFC-3339 parser rejects outright.
    offset_minutes = _offset_minutes(record.get("start_time_utc_offset"))
    start = _start_instant(start_text, offset_minutes)
    if start is None:
        return None

    # Returned inline by `GET /v3/exercises?zones=true`.
    zones = _zone_minutes(record.get("heart_rate_zones"))
    if zones is None:
        return None
    load = session_load(zones)

    duration_min = _duration_minutes(record.get("duration"))
    end = start + timedelta(minutes=duration_min) if duration_min is not None else start
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

    The vendor's ``zone`` array: five objects, each with an ISO-8601 ``in-zone``
    duration. All five must be present and in range — a partial set is dropped
    rather than zero-filled, since a missing zone is unknown, not zero.
    """
    if not isinstance(raw, list) or len(raw) != 5:
        return None

    values: list[float] = []
    for entry in raw:
        minutes = _zone_entry_minutes(entry)
        if minutes is None:
            return None
        values.append(minutes)

    if any(not 0.0 <= v <= _MAX_ZONE_MINUTES for v in values):
        return None
    return ZoneMinutes(z1=values[0], z2=values[1], z3=values[2], z4=values[3], z5=values[4])


def _zone_entry_minutes(entry: object) -> float | None:
    """Minutes in one zone, from the vendor's hyphenated ``in-zone`` field."""
    if not isinstance(entry, dict):
        return None
    return _duration_minutes(entry.get("in-zone"))


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


def _offset_minutes(raw: object) -> int | None:
    """The vendor's ``start_time_utc_offset``, in minutes, or None.

    Bounded to ±24h: a value outside that is not a real zone offset, and
    trusting it would shift the exercise onto the wrong local day.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if -_MAX_OFFSET_MINUTES <= raw <= _MAX_OFFSET_MINUTES else None


def _start_instant(start_text: str, offset_minutes: int | None) -> datetime | None:
    """Combine the naive local ``start_time`` with its offset into a UTC instant.

    Returns None when the timestamp is unparseable or already carries a zone the
    vendor is not documented to send — the record is dropped rather than pinned
    to a guessed timezone, which would misdate the workout.
    """
    try:
        parsed = datetime.fromisoformat(start_text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return None
    if offset_minutes is None:
        # No offset to anchor the wall-clock reading; the instant is unknowable.
        return None
    return (parsed - timedelta(minutes=offset_minutes)).replace(tzinfo=UTC)


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
