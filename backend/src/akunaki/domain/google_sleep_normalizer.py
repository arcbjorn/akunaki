"""Google Health v4 sleep normalizer: segment payload to canonical sleep facts.

Pure: no I/O, no clock. Every timestamp comes from the payload, never
``now()`` — a re-run over the same raw revision must produce byte-identical
facts, which is what makes normalization safely repeatable.

Google Health delivers sleep as **stage segments** (``com.google.sleep.segment``
data points), each a ``{startTime, endTime, sleepType}`` slice, unlike Oura's
one-record-per-night model. This normalizer **aggregates the segments of one
night into a single canonical session**:

- **Wake-date grouping.** Segments are grouped by the local date of their *end*
  (the canonical wake-date rule), so a 23:00→07:00 night's segments all land on
  the morning it ended, and a nap is its own group.
- **Stage minutes** are summed per ``sleepType``; total sleep is the sum of all
  non-awake stages; time-in-bed is the session span (first onset to last wake).
- **Quality** degrades when no recognized stage detail is present, rather than
  presenting a partial night as complete.

The output ``SleepFact`` contract is shared with the Oura normalizer, so both
providers write the same canonical ``sleep_sessions`` shape.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from akunaki.domain.jobs import parse_utc_rfc3339, to_utc_rfc3339
from akunaki.domain.sleep_normalizer import NormalizationError, SleepFact

NORMALIZER_VERSION = "google_sleep_v0.1.0"
ENTITY_TYPE = "sleep_session"

# A grouped session under this many minutes of sleep is treated as a nap.
NAP_MAX_MINUTES = 180.0

# Google Health sleep-stage vocabulary -> canonical stage bucket. Values absent
# here (unspecified, out-of-bed) contribute to neither sleep nor a stage.
_STAGE_MAP = {
    "SLEEP_STAGE_LIGHT": "light",
    "SLEEP_STAGE_DEEP": "deep",
    "SLEEP_STAGE_REM": "rem",
    "SLEEP_STAGE_AWAKE": "awake",
}


def normalize_google_sleep_payload(payload_text: str) -> list[SleepFact]:
    """Normalize a Google Health sleep-segment page into canonical facts.

    Raises :class:`NormalizationError` for a structurally unusable payload;
    individual segments that are unusable are skipped rather than failing the
    whole page, so one bad segment cannot block a night's ingestion.
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
    if not isinstance(raw_points, list):
        msg = "payload has no dataPoints array"
        raise NormalizationError(msg)

    # Group usable segments by their wake local-date, preserving order.
    groups: dict[str, list[_Segment]] = defaultdict(list)
    for point in raw_points:
        if not isinstance(point, dict):
            continue
        segment = _parse_segment(point)
        if segment is not None:
            groups[segment.wake_local_date].append(segment)

    facts: list[SleepFact] = []
    for wake_date, segments in groups.items():
        fact = _session_from_segments(wake_date, segments)
        if fact is not None:
            facts.append(fact)
    # Deterministic order regardless of vendor page ordering.
    facts.sort(key=lambda f: f.local_health_day)
    return facts


class _Segment:
    """One parsed sleep-stage segment (internal to aggregation)."""

    __slots__ = ("end", "offset_minutes", "stage", "start", "wake_local_date")

    def __init__(
        self,
        *,
        start: datetime,
        end: datetime,
        offset_minutes: int | None,
        wake_local_date: str,
        stage: str | None,
    ) -> None:
        self.start = start
        self.end = end
        self.offset_minutes = offset_minutes
        self.wake_local_date = wake_local_date
        self.stage = stage


def _parse_segment(point: dict[str, Any]) -> _Segment | None:
    """Parse one data point into a segment, or None when unusable."""
    start_text = point.get("startTime")
    end_text = point.get("endTime")
    if not isinstance(start_text, str) or not isinstance(end_text, str):
        return None
    try:
        start = parse_utc_rfc3339(start_text)
        end = parse_utc_rfc3339(end_text)
    except ValueError:
        return None
    if end <= start:
        return None

    stage_raw = point.get("sleepType")
    stage = _STAGE_MAP.get(stage_raw) if isinstance(stage_raw, str) else None

    offset_minutes = _offset_minutes(end_text)
    wake_local_date = _wake_local_date(end, offset_minutes)
    return _Segment(
        start=start,
        end=end,
        offset_minutes=offset_minutes,
        wake_local_date=wake_local_date,
        stage=stage,
    )


def _session_from_segments(wake_date: str, segments: list[_Segment]) -> SleepFact | None:
    """Aggregate one night's segments into a canonical sleep session."""
    if not segments:
        return None

    session_start = min(seg.start for seg in segments)
    session_end = max(seg.end for seg in segments)
    if session_end <= session_start:
        return None

    stage_minutes: dict[str, float] = {"light": 0.0, "deep": 0.0, "rem": 0.0, "awake": 0.0}
    for seg in segments:
        if seg.stage is not None:
            stage_minutes[seg.stage] += _span_minutes(seg.start, seg.end)

    light = _nonzero_or_none(stage_minutes["light"])
    deep = _nonzero_or_none(stage_minutes["deep"])
    rem = _nonzero_or_none(stage_minutes["rem"])
    awake = _nonzero_or_none(stage_minutes["awake"])

    sleep_minutes = stage_minutes["light"] + stage_minutes["deep"] + stage_minutes["rem"]
    has_stages = sleep_minutes > 0.0
    # With recognized sleep stages, duration is their sum; otherwise fall back to
    # the session span (a night present but with no stage detail).
    duration_min = (
        round(sleep_minutes, 3) if has_stages else _span_minutes(session_start, session_end)
    )
    time_in_bed_min = _span_minutes(session_start, session_end)

    quality, confidence = _quality_for(has_stages=has_stages)
    is_nap = duration_min <= NAP_MAX_MINUTES

    # The wake-date is the stable session identity: one main session per night.
    offset_minutes = segments[-1].offset_minutes
    fact = SleepFact(
        vendor_record_id=f"google_sleep:{wake_date}",
        start_utc=to_utc_rfc3339(session_start),
        end_utc=to_utc_rfc3339(session_end),
        local_health_day=wake_date,
        iana_timezone=None,
        source_offset_minutes=offset_minutes,
        duration_min=duration_min,
        time_in_bed_min=time_in_bed_min,
        efficiency_pct=_efficiency(duration_min, time_in_bed_min),
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


def _efficiency(duration_min: float, time_in_bed_min: float) -> float | None:
    """Sleep-efficiency percent, or None when undefined or out of range."""
    if time_in_bed_min <= 0.0:
        return None
    pct = round(duration_min / time_in_bed_min * 100.0, 3)
    return pct if 0.0 <= pct <= 100.0 else None


def _nonzero_or_none(minutes: float) -> float | None:
    """A stage total, or None when the stage never appeared (never a fake 0)."""
    return round(minutes, 3) if minutes > 0.0 else None


def _quality_for(*, has_stages: bool) -> tuple[str, float]:
    """Grade a session: missing stage detail lowers quality, never hides it."""
    if has_stages:
        return "high", 0.9
    # A night present from segments but with no recognized stage detail.
    return "low", 0.4


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


def _span_minutes(start: datetime, end: datetime) -> float:
    return round((end - start).total_seconds() / 60.0, 3)


def _with_content_hash(fact: SleepFact) -> SleepFact:
    """Attach a hash over the normalized values, for change detection."""
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
