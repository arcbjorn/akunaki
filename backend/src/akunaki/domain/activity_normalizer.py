"""Daily-activity normalizers: per-provider payloads to canonical facts.

Pure: no I/O, no clock. Every timestamp comes from the payload, never
``now()`` — a re-run over the same raw revision produces byte-identical facts.

Three providers report daily activity in three shapes, and each has its own
reader below; all of them emit the same :class:`ActivityFact`, so downstream
code never learns which vendor supplied a day:

- **Google Health** — per-day data points with an explicit window.
- **Oura** — a ``day`` field plus per-intensity activity **seconds**.
- **Polar** — a bare array of intra-day summaries with naive local timestamps,
  aggregated per local day.

Each keeps ``steps`` (an integer count) and ``active_minutes`` (moderate+
minutes). A day with neither signal yields no fact — an empty-signal row would
violate the detail table's "at least one" invariant and carry nothing. Values
are sanity-bounded.

Facts are identified **per provider** (see ``ActivityFact.fact_key``), so two
providers covering one day compete for source selection rather than superseding
each other.

The ``activity`` *score* stays blocked (no accepted formula); these facts feed
the **low-activity anomaly** and future activity surfaces only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from akunaki.domain.jobs import parse_utc_rfc3339
from akunaki.domain.sleep_normalizer import NormalizationError

NORMALIZER_VERSION = "google_activity_v0.1.0"
ENTITY_TYPE = "daily_activity"

# Sanity bounds; a value outside these is dropped (the signal, not the record).
_MAX_STEPS = 200_000
_MAX_ACTIVE_MINUTES = 24.0 * 60.0


@dataclass(frozen=True, slots=True)
class ActivityFact:
    """One day's canonical activity totals."""

    vendor_record_id: str
    start_utc: str
    end_utc: str
    local_health_day: str
    source_offset_minutes: int | None
    steps: int | None
    active_minutes: float | None
    quality: str
    confidence: float
    content_hash: str

    @property
    def fact_key(self) -> str:
        """Stable logical identity across versions of this day's activity.

        ``vendor_record_id`` is namespaced by provider (``activity:oura:<day>``)
        because a daily aggregate has no vendor-assigned id to key on, unlike a
        sleep session. Keying on the day alone made every provider's record for
        a day the *same* logical fact, so the second one to sync superseded the
        first as a new version: whichever provider happened to sync last won,
        the other was marked not-current, and the source policy never saw a
        contest at all. Per-provider identity keeps both facts current so the
        policy can pick the authoritative one and retain the loser as a
        candidate.
        """
        return f"{ENTITY_TYPE}:{self.vendor_record_id}"


def normalize_activity_payload(payload_text: str) -> list[ActivityFact]:
    """Normalize a Google Health daily-activity page into canonical facts.

    Raises :class:`NormalizationError` for a structurally unusable payload;
    individual points that are unusable (bad times, no signal) are skipped
    rather than failing the whole page.
    """
    try:
        parsed = json.loads(payload_text)
    except ValueError as exc:
        msg = "payload is not valid json"
        raise NormalizationError(msg) from exc

    if not isinstance(parsed, dict):
        msg = "payload root must be an object"
        raise NormalizationError(msg)

    raw_points = parsed.get("dataPoints")
    if isinstance(raw_points, list):
        points = raw_points
    elif "startTime" in parsed:
        # A per-record slice from the raw layer, not a collection page.
        points = [parsed]
    else:
        msg = "payload has no dataPoints array"
        raise NormalizationError(msg)

    facts: list[ActivityFact] = []
    for point in points:
        if not isinstance(point, dict):
            continue
        fact = _normalize_point(point)
        if fact is not None:
            facts.append(fact)
    facts.sort(key=lambda f: f.local_health_day)
    return facts


def _normalize_point(point: dict[str, Any]) -> ActivityFact | None:
    """Normalize one daily-activity point, or None when unusable."""
    start_text = point.get("startTime")
    end_text = point.get("endTime")
    if not isinstance(start_text, str) or not isinstance(end_text, str):
        return None
    try:
        start = parse_utc_rfc3339(start_text)
        end = parse_utc_rfc3339(end_text)
    except ValueError:
        return None
    if end < start:
        return None

    steps = _clean_steps(point.get("steps"))
    active_minutes = _clean_active_minutes(point.get("activeMinutes"))
    if steps is None and active_minutes is None:
        # No usable signal: nothing to store (the detail table forbids it).
        return None

    offset_minutes = _offset_minutes(start_text)
    local_day = _local_date(start, offset_minutes)
    quality, confidence = _quality_for(steps=steps, active_minutes=active_minutes)

    fact = ActivityFact(
        vendor_record_id=f"activity:google_health:{local_day}",
        start_utc=_to_z(start),
        end_utc=_to_z(end),
        local_health_day=local_day,
        source_offset_minutes=offset_minutes,
        steps=steps,
        active_minutes=active_minutes,
        quality=quality,
        confidence=confidence,
        content_hash="",
    )
    return _with_content_hash(fact)


def _clean_steps(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    ivalue = int(value)
    if ivalue < 0 or ivalue > _MAX_STEPS:
        return None
    return ivalue


def _clean_active_minutes(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    fvalue = float(value)
    if fvalue < 0.0 or fvalue > _MAX_ACTIVE_MINUTES:
        return None
    return round(fvalue, 3)


def _quality_for(*, steps: int | None, active_minutes: float | None) -> tuple[str, float]:
    """Both signals present is higher quality than one alone."""
    if steps is not None and active_minutes is not None:
        return "high", 0.9
    return "medium", 0.7


def _local_date(start_utc: datetime, offset_minutes: int | None) -> str:
    local = start_utc + timedelta(minutes=offset_minutes or 0)
    return local.date().isoformat()


def _offset_minutes(timestamp_text: str) -> int | None:
    try:
        parsed = datetime.fromisoformat(timestamp_text)
    except ValueError:
        return None
    offset = parsed.utcoffset()
    if offset is None:
        return None
    return int(offset.total_seconds() // 60)


def _to_z(value: datetime) -> str:
    """RFC3339 with a Z suffix (the value is already UTC after parsing)."""
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _with_content_hash(fact: ActivityFact) -> ActivityFact:
    """Attach a hash over the normalized values, for change detection."""
    material = json.dumps(
        {
            "vendor_record_id": fact.vendor_record_id,
            "start_utc": fact.start_utc,
            "end_utc": fact.end_utc,
            "local_health_day": fact.local_health_day,
            "steps": fact.steps,
            "active_minutes": fact.active_minutes,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return ActivityFact(
        vendor_record_id=fact.vendor_record_id,
        start_utc=fact.start_utc,
        end_utc=fact.end_utc,
        local_health_day=fact.local_health_day,
        source_offset_minutes=fact.source_offset_minutes,
        steps=fact.steps,
        active_minutes=fact.active_minutes,
        quality=fact.quality,
        confidence=fact.confidence,
        content_hash=digest,
    )


# ---------------------------------------------------------------------------
# Oura daily activity (`oura_activity.v2`)
# ---------------------------------------------------------------------------

OURA_NORMALIZER_VERSION = "oura_activity_v0.1.0"


def normalize_oura_activity_payload(payload_text: str) -> list[ActivityFact]:
    """Normalize an Oura ``daily_activity`` page into canonical facts.

    Oura reports a **`day`** directly, so the local health day is read rather
    than derived from an instant plus an offset — the vendor already resolved
    the boundary and re-deriving it could disagree with the rest of the record.

    Active minutes are summed from Oura's medium + high activity **seconds**
    (`medium_activity_time`, `high_activity_time`). Low and sedentary time are
    deliberately excluded: the canonical field is *moderate-and-above* minutes,
    matching what the low-activity anomaly and every other provider mean by it.
    """
    points = _oura_points(payload_text)
    facts: list[ActivityFact] = []
    for point in points:
        fact = _normalize_oura_point(point)
        if fact is not None:
            facts.append(fact)
    facts.sort(key=lambda f: f.local_health_day)
    return facts


def _oura_points(payload_text: str) -> list[Any]:
    try:
        parsed = json.loads(payload_text)
    except ValueError as exc:
        msg = "payload is not valid json"
        raise NormalizationError(msg) from exc
    if not isinstance(parsed, dict):
        msg = "payload root must be an object"
        raise NormalizationError(msg)
    raw = parsed.get("data")
    if isinstance(raw, list):
        return raw
    if "day" in parsed:
        # A per-record slice from the raw layer, not a collection page.
        return [parsed]
    msg = "payload has no data array"
    raise NormalizationError(msg)


def _normalize_oura_point(point: dict[str, Any]) -> ActivityFact | None:
    """Normalize one Oura daily-activity record, or None when unusable."""
    if not isinstance(point, dict):
        return None
    day = point.get("day")
    if not isinstance(day, str) or not day:
        return None

    timestamp = point.get("timestamp")
    offset_minutes = _offset_minutes(timestamp) if isinstance(timestamp, str) else None

    steps = _clean_steps(point.get("steps"))
    active_minutes = _oura_active_minutes(point)
    if steps is None and active_minutes is None:
        return None

    # The record describes a whole local day; bound it by that day rather than
    # inventing sub-day precision the vendor did not report.
    start_utc, end_utc = _day_bounds(day, offset_minutes)
    quality, confidence = _quality_for(steps=steps, active_minutes=active_minutes)

    fact = ActivityFact(
        vendor_record_id=f"activity:oura:{day}",
        start_utc=start_utc,
        end_utc=end_utc,
        local_health_day=day,
        source_offset_minutes=offset_minutes,
        steps=steps,
        active_minutes=active_minutes,
        quality=quality,
        confidence=confidence,
        content_hash="",
    )
    return _with_content_hash(fact)


def _oura_active_minutes(point: dict[str, Any]) -> float | None:
    """Moderate+ active minutes from Oura's per-intensity **seconds**."""
    total_seconds = 0.0
    seen = False
    for key in ("medium_activity_time", "high_activity_time"):
        value = point.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        if value < 0:
            continue
        total_seconds += float(value)
        seen = True
    if not seen:
        return None
    return _clean_active_minutes(total_seconds / 60.0)


# ---------------------------------------------------------------------------
# Polar daily activity (`polar_activity.v1`)
# ---------------------------------------------------------------------------

POLAR_NORMALIZER_VERSION = "polar_activity_v0.1.0"


def normalize_polar_activity_payload(payload_text: str) -> list[ActivityFact]:
    """Normalize a Polar ``users/activities`` page into canonical facts.

    Polar returns a **bare JSON array** of activity summaries whose
    ``start_time``/``end_time`` are naive local wall-clock strings — the same
    trap the workout normalizer handles. A record is assigned to the local date
    of its ``start_time`` as written, since that string *is* the user's local
    time; no offset is claimed, because the payload carries none.

    Polar splits one day across several records (it emits a new summary as the
    day progresses), so records are **aggregated per local day**: steps and
    active minutes are summed rather than the last record overwriting the day.
    """
    records = _polar_records(payload_text)

    by_day: dict[str, dict[str, float | None]] = {}
    for record in records:
        parsed = _parse_polar_record(record)
        if parsed is None:
            continue
        day, steps, minutes = parsed
        bucket = by_day.setdefault(day, {"steps": None, "minutes": None})
        if steps is not None:
            bucket["steps"] = (bucket["steps"] or 0) + steps
        if minutes is not None:
            bucket["minutes"] = (bucket["minutes"] or 0.0) + minutes

    facts: list[ActivityFact] = []
    for day in sorted(by_day):
        steps_total = by_day[day]["steps"]
        minutes_total = by_day[day]["minutes"]
        steps = _clean_steps(steps_total) if steps_total is not None else None
        active_minutes = _clean_active_minutes(minutes_total) if minutes_total is not None else None
        if steps is None and active_minutes is None:
            continue
        start_utc, end_utc = _day_bounds(day, None)
        quality, confidence = _quality_for(steps=steps, active_minutes=active_minutes)
        facts.append(
            _with_content_hash(
                ActivityFact(
                    vendor_record_id=f"activity:polar:{day}",
                    start_utc=start_utc,
                    end_utc=end_utc,
                    local_health_day=day,
                    # Polar's timestamps are naive local time with no offset in
                    # the body; claiming one would be a guess.
                    source_offset_minutes=None,
                    steps=steps,
                    active_minutes=active_minutes,
                    quality=quality,
                    confidence=confidence,
                    content_hash="",
                )
            )
        )
    return facts


def _polar_records(payload_text: str) -> list[Any]:
    try:
        parsed = json.loads(payload_text)
    except ValueError as exc:
        msg = "payload is not valid json"
        raise NormalizationError(msg) from exc
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict) and "start_time" in parsed:
        # A per-record slice from the raw layer.
        return [parsed]
    msg = "payload must be an array of activity summaries"
    raise NormalizationError(msg)


def _parse_polar_record(record: object) -> tuple[str, int | None, float | None] | None:
    """Return ``(local_day, steps, active_minutes)`` for one record."""
    if not isinstance(record, dict):
        return None
    start_text = record.get("start_time")
    if not isinstance(start_text, str) or not start_text:
        return None
    try:
        # Naive local wall-clock time: the date part is the local day.
        local_day = datetime.fromisoformat(start_text).date().isoformat()
    except ValueError:
        return None

    steps = _clean_steps(record.get("steps"))
    active_minutes = _iso8601_duration_minutes(record.get("active_duration"))
    if steps is None and active_minutes is None:
        return None
    return local_day, steps, active_minutes


def _iso8601_duration_minutes(value: object) -> float | None:
    """Parse Polar's ISO-8601 duration (``PT27M30S``) into minutes."""
    if not isinstance(value, str) or not value.startswith("PT"):
        return None
    total = 0.0
    number = ""
    for char in value[2:]:
        if char.isdigit() or char == ".":
            number += char
            continue
        if not number:
            return None
        try:
            magnitude = float(number)
        except ValueError:
            return None
        if char == "H":
            total += magnitude * 60.0
        elif char == "M":
            total += magnitude
        elif char == "S":
            total += magnitude / 60.0
        else:
            return None
        number = ""
    if number:
        # Trailing digits with no unit are unparseable rather than assumed.
        return None
    return _clean_active_minutes(total)


def _day_bounds(local_day: str, offset_minutes: int | None) -> tuple[str, str]:
    """UTC bounds of a whole local day, as Z-suffixed RFC3339 strings."""
    try:
        day = datetime.fromisoformat(local_day)
    except ValueError:
        day = datetime.fromisoformat(f"{local_day}T00:00:00")
    shift = timedelta(minutes=offset_minutes or 0)
    start = day.replace(tzinfo=UTC) - shift
    return _to_z(start), _to_z(start + timedelta(days=1))
