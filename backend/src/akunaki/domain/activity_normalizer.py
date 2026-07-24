"""Google Health activity normalizer: daily-activity payload to canonical facts.

Pure: no I/O, no clock. Every timestamp comes from the payload, never
``now()`` — a re-run over the same raw revision produces byte-identical facts.

Google Health exposes daily activity aggregates (steps, moderate+ active
minutes) as per-day data points. This normalizer assigns each to the **local
date** of its window and keeps ``steps`` (an integer count) and
``active_minutes`` (moderate+ minutes). A day with neither signal yields no
fact — an empty-signal row would violate the detail table's "at least one"
invariant and carry nothing. Values are sanity-bounded.

The ``activity`` *score* stays blocked (no accepted formula); these facts feed
the **low-activity anomaly** and future activity surfaces only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
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
        """Stable logical identity across versions of this day's activity."""
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
        vendor_record_id=f"activity:{local_day}",
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
