"""Golden tests for the Google Health sleep-segment normalizer.

Hand-computed from the segment aggregation rules: stage minutes summed per
sleepType, total sleep = non-awake stages, time-in-bed = session span, wake-date
grouping, and quality degradation without stage detail.
"""

from __future__ import annotations

import json

import pytest

from akunaki.domain.google_sleep_normalizer import (
    NORMALIZER_VERSION,
    normalize_google_sleep_payload,
)
from akunaki.domain.sleep_normalizer import NormalizationError


def _page(*segments: dict[str, str]) -> str:
    return json.dumps({"dataPoints": list(segments)})


def _seg(start: str, end: str, stage: str | None) -> dict[str, str]:
    point: dict[str, str] = {"startTime": start, "endTime": end}
    if stage is not None:
        point["sleepType"] = stage
    return point


def test_normalizer_version_is_pinned() -> None:
    assert NORMALIZER_VERSION == "google_sleep_v0.1.0"


def test_aggregates_one_night_from_stage_segments() -> None:
    # 60 light + 60 deep + 300 rem = 420 sleep; +30 awake -> 450 min in bed.
    page = _page(
        _seg("2026-07-22T00:00:00+02:00", "2026-07-22T01:00:00+02:00", "SLEEP_STAGE_LIGHT"),
        _seg("2026-07-22T01:00:00+02:00", "2026-07-22T02:00:00+02:00", "SLEEP_STAGE_DEEP"),
        _seg("2026-07-22T02:00:00+02:00", "2026-07-22T07:00:00+02:00", "SLEEP_STAGE_REM"),
        _seg("2026-07-22T07:00:00+02:00", "2026-07-22T07:30:00+02:00", "SLEEP_STAGE_AWAKE"),
    )
    facts = normalize_google_sleep_payload(page)
    assert len(facts) == 1
    fact = facts[0]
    assert fact.local_health_day == "2026-07-22"
    assert fact.duration_min == pytest.approx(420.0)
    assert fact.time_in_bed_min == pytest.approx(450.0)
    assert fact.light_min == pytest.approx(60.0)
    assert fact.deep_min == pytest.approx(60.0)
    assert fact.rem_min == pytest.approx(300.0)
    assert fact.awake_min == pytest.approx(30.0)
    assert fact.efficiency_pct == pytest.approx(round(420.0 / 450.0 * 100.0, 3))
    assert fact.is_nap is False
    assert fact.quality == "high"
    # Session identity is the wake-date, so a re-run maps to the same fact.
    assert fact.vendor_record_id == "google_sleep:2026-07-22"
    assert fact.fact_key == "sleep_session:google_sleep:2026-07-22"


def test_wake_date_groups_across_midnight() -> None:
    # A 23:00 -> 07:00 night: all segments belong to the wake morning.
    page = _page(
        _seg("2026-07-21T23:00:00+02:00", "2026-07-22T03:00:00+02:00", "SLEEP_STAGE_LIGHT"),
        _seg("2026-07-22T03:00:00+02:00", "2026-07-22T06:30:00+02:00", "SLEEP_STAGE_DEEP"),
    )
    facts = normalize_google_sleep_payload(page)
    assert len(facts) == 1
    assert facts[0].local_health_day == "2026-07-22"
    # 240 light + 210 deep = 450 sleep minutes.
    assert facts[0].duration_min == pytest.approx(450.0)


def test_two_nights_become_two_sorted_facts() -> None:
    page = _page(
        _seg("2026-07-23T01:00:00+02:00", "2026-07-23T05:00:00+02:00", "SLEEP_STAGE_LIGHT"),
        _seg("2026-07-22T01:00:00+02:00", "2026-07-22T05:00:00+02:00", "SLEEP_STAGE_LIGHT"),
    )
    facts = normalize_google_sleep_payload(page)
    assert [f.local_health_day for f in facts] == ["2026-07-22", "2026-07-23"]


def test_unknown_stage_counts_as_in_bed_but_not_sleep() -> None:
    # An unspecified stage adds to the span (time-in-bed) but no stage bucket.
    page = _page(
        _seg("2026-07-22T01:00:00+02:00", "2026-07-22T05:00:00+02:00", "SLEEP_STAGE_LIGHT"),
        _seg("2026-07-22T05:00:00+02:00", "2026-07-22T05:30:00+02:00", "SLEEP_STAGE_UNSPECIFIED"),
    )
    facts = normalize_google_sleep_payload(page)
    fact = facts[0]
    # 240 light sleep; span 01:00 -> 05:30 is 270 in bed.
    assert fact.duration_min == pytest.approx(240.0)
    assert fact.time_in_bed_min == pytest.approx(270.0)
    assert fact.light_min == pytest.approx(240.0)
    assert fact.deep_min is None
    assert fact.awake_min is None


def test_no_recognized_stages_is_low_quality() -> None:
    # A night present only as unspecified segments: duration from span, low q.
    page = _page(
        _seg("2026-07-22T01:00:00+02:00", "2026-07-22T06:00:00+02:00", "SLEEP_STAGE_UNSPECIFIED"),
    )
    facts = normalize_google_sleep_payload(page)
    fact = facts[0]
    assert fact.duration_min == pytest.approx(300.0)  # falls back to span
    assert fact.quality == "low"
    assert fact.light_min is None


def test_short_night_is_a_nap() -> None:
    page = _page(
        _seg("2026-07-22T13:00:00+02:00", "2026-07-22T14:00:00+02:00", "SLEEP_STAGE_LIGHT"),
    )
    facts = normalize_google_sleep_payload(page)
    assert facts[0].is_nap is True


def test_re_run_is_byte_identical() -> None:
    page = _page(
        _seg("2026-07-22T01:00:00+02:00", "2026-07-22T05:00:00+02:00", "SLEEP_STAGE_LIGHT"),
    )
    first = normalize_google_sleep_payload(page)
    second = normalize_google_sleep_payload(page)
    assert first[0].content_hash == second[0].content_hash


def test_content_hash_changes_with_the_values() -> None:
    a = normalize_google_sleep_payload(
        _page(_seg("2026-07-22T01:00:00+02:00", "2026-07-22T05:00:00+02:00", "SLEEP_STAGE_LIGHT"))
    )
    b = normalize_google_sleep_payload(
        _page(_seg("2026-07-22T01:00:00+02:00", "2026-07-22T06:00:00+02:00", "SLEEP_STAGE_LIGHT"))
    )
    assert a[0].content_hash != b[0].content_hash


def test_bad_segments_are_skipped_not_fatal() -> None:
    page = _page(
        _seg(
            "2026-07-22T05:00:00+02:00", "2026-07-22T01:00:00+02:00", "SLEEP_STAGE_LIGHT"
        ),  # reversed
        {"startTime": "not-a-time", "endTime": "also-bad", "sleepType": "SLEEP_STAGE_DEEP"},  # type: ignore[arg-type]
        _seg("2026-07-22T01:00:00+02:00", "2026-07-22T05:00:00+02:00", "SLEEP_STAGE_REM"),
    )
    facts = normalize_google_sleep_payload(page)
    # Only the one usable segment survives, as a valid night.
    assert len(facts) == 1
    assert facts[0].rem_min == pytest.approx(240.0)


def test_malformed_payload_raises() -> None:
    with pytest.raises(NormalizationError):
        normalize_google_sleep_payload("not json")
    with pytest.raises(NormalizationError):
        normalize_google_sleep_payload(json.dumps({"no": "dataPoints"}))


def test_empty_page_yields_no_facts() -> None:
    assert normalize_google_sleep_payload(_page()) == []
