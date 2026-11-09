"""Google Health v4 vitals normalizer: daily heart summaries to overnight-vitals facts.

Pure: no I/O, no clock. Every value comes from the payload, so a re-run over
the same raw revision produces byte-identical facts.

A band publishes its overnight heart work to Google Health as two **daily
summary** data types, ``daily-resting-heart-rate`` (``beatsPerMinute``) and
``daily-heart-rate-variability`` (``averageHeartRateVariabilityMilliseconds``,
an RMSSD). They arrive on separate streams, so one page carries one signal per
day; both normalize to the same canonical overnight-vitals fact, keyed by the
summary's civil ``date``, and the versioned write merges the halves as each
lands (see ``merged_vitals_fact``).

The summary carries no clock offset, so the fact's span is the civil day
expressed as if UTC and ``source_offset_minutes`` is None: the date is the
vendor's own local attribution and is what the fact is keyed by.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from akunaki.domain.sleep_normalizer import NormalizationError
from akunaki.domain.vitals_normalizer import VitalsFact, build_vitals_fact

NORMALIZER_VERSION = "google_vitals_v0.1.0"

__all__ = ["NORMALIZER_VERSION", "normalize_google_vitals_payload", "vendor_record_id_for"]


def vendor_record_id_for(local_health_day: str) -> str:
    """The shared identity both heart streams write a day's vitals under."""
    return f"google_vitals:{local_health_day}"


def normalize_google_vitals_payload(payload_text: str) -> list[VitalsFact]:
    """Normalize a daily heart-summary page into overnight-vitals facts, one per day.

    Raises :class:`NormalizationError` for a structurally unusable payload; a
    point without a usable date or value is skipped, so one bad summary cannot
    block the page.
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

    by_day: dict[str, dict[str, float]] = {}
    for point in raw_points:
        if not isinstance(point, dict):
            continue
        resting = point.get("dailyRestingHeartRate")
        if isinstance(resting, dict):
            day = _civil_day(resting.get("date"))
            bpm = _number(resting.get("beatsPerMinute"))
            if day is not None and bpm is not None:
                by_day.setdefault(day, {})["resting_hr"] = bpm
        variability = point.get("dailyHeartRateVariability")
        if isinstance(variability, dict):
            day = _civil_day(variability.get("date"))
            # RMSSD over the night is the canonical HRV; the deep-sleep RMSSD
            # stands in only when the average is absent.
            rmssd = _number(variability.get("averageHeartRateVariabilityMilliseconds"))
            if rmssd is None:
                rmssd = _number(
                    variability.get("deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds")
                )
            if day is not None and rmssd is not None:
                by_day.setdefault(day, {})["hrv"] = rmssd

    facts: list[VitalsFact] = []
    for day in sorted(by_day):
        signals = by_day[day]
        next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        fact = build_vitals_fact(
            vendor_record_id=vendor_record_id_for(day),
            start_utc=f"{day}T00:00:00Z",
            end_utc=f"{next_day}T00:00:00Z",
            local_health_day=day,
            source_offset_minutes=None,
            hrv=signals.get("hrv"),
            resting_hr=signals.get("resting_hr"),
        )
        if fact is not None:
            facts.append(fact)
    return facts


def _civil_day(value: object) -> str | None:
    """A v4 ``Date`` object (``year``/``month``/``day`` integers) as ISO, or None."""
    if not isinstance(value, dict):
        return None
    parts = [value.get(key) for key in ("year", "month", "day")]
    if not all(isinstance(part, int) and not isinstance(part, bool) for part in parts):
        return None
    try:
        return date(*parts).isoformat()  # type: ignore[arg-type]
    except ValueError:
        return None


def _number(value: object) -> float | None:
    """A JSON number, or an int64 carried as a decimal string, as float."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None
