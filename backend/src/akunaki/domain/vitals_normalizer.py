"""Oura overnight-vitals normalizer: sleep payload to canonical vitals facts.

Pure: no I/O, no clock. Every timestamp comes from the payload, never
``now()`` — a re-run over the same raw revision produces byte-identical facts.

Overnight HRV (RMSSD ms), resting heart rate (bpm), temperature deviation (°C),
and respiration rate (breaths/min) ride along on Oura's main sleep record
(``average_hrv``, ``lowest_heart_rate``, ``temperature_deviation`` /
``readiness.temperature_deviation``, ``average_breath``). They are extracted
into their own canonical entity, keyed to the **wake date** exactly as the
sleep session is, so a night's vitals and its sleep share a local health day.

Only the **main** sleep bout carries meaningful overnight vitals; naps are
skipped. A record with none of the four signals yields no fact — an empty-signal
row would violate the detail table's "at least one" invariant and carry nothing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from akunaki.domain.jobs import parse_utc_rfc3339, to_utc_rfc3339

NORMALIZER_VERSION = "oura_vitals_v0.1.0"
ENTITY_TYPE = "overnight_vitals"

# A short bout is a nap; overnight vitals are read from the main sleep only.
NAP_MAX_MINUTES = 180.0

# Physiologic sanity bounds; a value outside these is dropped, not stored.
_HRV_MAX_MS = 500.0
_RHR_MIN_BPM = 20.0
_RHR_MAX_BPM = 200.0
# Temperature deviation is already relative to the user's own baseline; a
# plausible overnight departure is well within a few degrees Celsius.
_TEMP_DEV_MIN_C = -5.0
_TEMP_DEV_MAX_C = 5.0
# Overnight respiration for a healthy adult sits well inside this range.
_RESP_MIN_BPM = 3.0
_RESP_MAX_BPM = 60.0


class NormalizationError(Exception):
    """Payload could not be normalized. Carries no vendor body."""


@dataclass(frozen=True, slots=True)
class VitalsFact:
    """One canonical overnight-vitals reading ready to persist."""

    vendor_record_id: str
    start_utc: str
    end_utc: str
    local_health_day: str
    source_offset_minutes: int | None
    hrv_ms: float | None
    resting_hr_bpm: float | None
    temperature_deviation_c: float | None
    respiratory_rate_bpm: float | None
    quality: str
    confidence: float
    content_hash: str

    @property
    def fact_key(self) -> str:
        """Stable logical identity across versions of this reading."""
        return f"{ENTITY_TYPE}:{self.vendor_record_id}"


def normalize_vitals_payload(payload_text: str) -> list[VitalsFact]:
    """Normalize an Oura V2 sleep page into canonical overnight-vitals facts.

    Raises :class:`NormalizationError` for a structurally unusable payload;
    individual records that carry no vitals are skipped, so a nap or a
    vitals-less night cannot block ingestion.
    """
    try:
        parsed = json.loads(payload_text)
    except ValueError as exc:
        msg = "payload is not valid json"
        raise NormalizationError(msg) from exc

    if not isinstance(parsed, dict):
        msg = "payload root must be an object"
        raise NormalizationError(msg)

    raw_records = parsed.get("data")
    if isinstance(raw_records, list):
        records = raw_records
    elif "bedtime_start" in parsed or "bedtime_end" in parsed:
        records = [parsed]
    else:
        msg = "payload has no data array"
        raise NormalizationError(msg)

    facts: list[VitalsFact] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        fact = _normalize_record(record)
        if fact is not None:
            facts.append(fact)
    return facts


def _normalize_record(record: dict[str, Any]) -> VitalsFact | None:
    """Normalize one Oura sleep record's vitals, or None when there are none."""
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

    # Naps do not carry overnight vitals; skip them explicitly.
    sleep_type = record.get("type")
    span_min = (end - start).total_seconds() / 60.0
    is_nap = sleep_type == "nap" if isinstance(sleep_type, str) else span_min <= NAP_MAX_MINUTES
    if is_nap:
        return None

    hrv_ms = _bounded(record.get("average_hrv"), low=0.0, high=_HRV_MAX_MS)
    resting_hr_bpm = _bounded(record.get("lowest_heart_rate"), low=_RHR_MIN_BPM, high=_RHR_MAX_BPM)
    temperature_deviation_c = _bounded(
        _temperature_deviation(record), low=_TEMP_DEV_MIN_C, high=_TEMP_DEV_MAX_C
    )
    respiratory_rate_bpm = _bounded(
        record.get("average_breath"), low=_RESP_MIN_BPM, high=_RESP_MAX_BPM
    )
    if (
        hrv_ms is None
        and resting_hr_bpm is None
        and temperature_deviation_c is None
        and respiratory_rate_bpm is None
    ):
        # No overnight signal on this record; nothing to persist.
        return None

    offset_minutes = _offset_minutes(bedtime_end)
    local_day = _wake_local_date(end, offset_minutes)
    quality, confidence = _quality_for(
        hrv=hrv_ms,
        rhr=resting_hr_bpm,
        temp=temperature_deviation_c,
        resp=respiratory_rate_bpm,
    )

    fact = VitalsFact(
        vendor_record_id=vendor_id,
        start_utc=to_utc_rfc3339(start),
        end_utc=to_utc_rfc3339(end),
        local_health_day=local_day,
        source_offset_minutes=offset_minutes,
        hrv_ms=hrv_ms,
        resting_hr_bpm=resting_hr_bpm,
        temperature_deviation_c=temperature_deviation_c,
        respiratory_rate_bpm=respiratory_rate_bpm,
        quality=quality,
        confidence=confidence,
        content_hash="",
    )
    return _with_content_hash(fact)


def _temperature_deviation(record: dict[str, Any]) -> object:
    """Read the temperature deviation from either the flat or nested field.

    Oura reports it under ``readiness.temperature_deviation`` on the daily
    readiness object, but a per-record slice may carry it flat.
    """
    flat = record.get("temperature_deviation")
    if flat is not None:
        return flat
    readiness = record.get("readiness")
    if isinstance(readiness, dict):
        return readiness.get("temperature_deviation")
    return None


def _with_content_hash(fact: VitalsFact) -> VitalsFact:
    """Attach a hash over the normalized values, for change detection."""
    material = json.dumps(
        {
            "vendor_record_id": fact.vendor_record_id,
            "start_utc": fact.start_utc,
            "end_utc": fact.end_utc,
            "local_health_day": fact.local_health_day,
            "hrv_ms": fact.hrv_ms,
            "resting_hr_bpm": fact.resting_hr_bpm,
            "temperature_deviation_c": fact.temperature_deviation_c,
            "respiratory_rate_bpm": fact.respiratory_rate_bpm,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return VitalsFact(
        vendor_record_id=fact.vendor_record_id,
        start_utc=fact.start_utc,
        end_utc=fact.end_utc,
        local_health_day=fact.local_health_day,
        source_offset_minutes=fact.source_offset_minutes,
        hrv_ms=fact.hrv_ms,
        resting_hr_bpm=fact.resting_hr_bpm,
        temperature_deviation_c=fact.temperature_deviation_c,
        respiratory_rate_bpm=fact.respiratory_rate_bpm,
        quality=fact.quality,
        confidence=fact.confidence,
        content_hash=digest,
    )


def _wake_local_date(end_utc: datetime, offset_minutes: int | None) -> str:
    """Return the local date of **wake**, matching the sleep assignment rule."""
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


def _bounded(value: object, *, low: float, high: float) -> float | None:
    """Return a finite value within [low, high], else None (out of range dropped)."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    as_float = float(value)
    if not low <= as_float <= high:
        return None
    return as_float


def _quality_for(
    *, hrv: float | None, rhr: float | None, temp: float | None, resp: float | None
) -> tuple[str, float]:
    """Grade a reading by how many overnight signals are present."""
    present = sum(1 for value in (hrv, rhr, temp, resp) if value is not None)
    if present >= 2:
        return "high", 0.95
    return "medium", 0.7
