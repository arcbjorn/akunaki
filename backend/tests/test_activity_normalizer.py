"""Golden tests for the Google Health daily-activity normalizer."""

from __future__ import annotations

import json

import pytest

from akunaki.domain.activity_normalizer import (
    NORMALIZER_VERSION,
    normalize_activity_payload,
)
from akunaki.domain.sleep_normalizer import NormalizationError


def _page(*points: dict[str, object]) -> str:
    return json.dumps({"dataPoints": list(points)})


def _day(start: str, end: str, **signals: object) -> dict[str, object]:
    return {"startTime": start, "endTime": end, **signals}


def test_normalizer_version_is_pinned() -> None:
    assert NORMALIZER_VERSION == "google_activity_v0.1.0"


def test_both_signals_present_is_high_quality() -> None:
    page = _page(
        _day(
            "2026-07-22T00:00:00+02:00",
            "2026-07-23T00:00:00+02:00",
            steps=8500,
            activeMinutes=42.5,
        )
    )
    facts = normalize_activity_payload(page)
    assert len(facts) == 1
    fact = facts[0]
    assert fact.local_health_day == "2026-07-22"
    assert fact.steps == 8500
    assert fact.active_minutes == pytest.approx(42.5)
    assert fact.quality == "high"
    assert fact.vendor_record_id == "activity:2026-07-22"
    assert fact.fact_key == "daily_activity:activity:2026-07-22"


def test_steps_only_is_medium_quality() -> None:
    page = _page(_day("2026-07-22T00:00:00Z", "2026-07-23T00:00:00Z", steps=12000))
    fact = normalize_activity_payload(page)[0]
    assert fact.steps == 12000
    assert fact.active_minutes is None
    assert fact.quality == "medium"


def test_active_minutes_only_records() -> None:
    page = _page(_day("2026-07-22T00:00:00Z", "2026-07-23T00:00:00Z", activeMinutes=30))
    fact = normalize_activity_payload(page)[0]
    assert fact.steps is None
    assert fact.active_minutes == pytest.approx(30.0)


def test_no_signal_day_is_dropped() -> None:
    page = _page(_day("2026-07-22T00:00:00Z", "2026-07-23T00:00:00Z"))
    assert normalize_activity_payload(page) == []


def test_days_are_sorted() -> None:
    page = _page(
        _day("2026-07-23T00:00:00Z", "2026-07-24T00:00:00Z", steps=1),
        _day("2026-07-21T00:00:00Z", "2026-07-22T00:00:00Z", steps=2),
    )
    facts = normalize_activity_payload(page)
    assert [f.local_health_day for f in facts] == ["2026-07-21", "2026-07-23"]


def test_negative_and_huge_values_are_dropped() -> None:
    # A negative step count and an implausibly large one are both dropped; with
    # no other signal the day is omitted entirely.
    page = _page(
        _day("2026-07-22T00:00:00Z", "2026-07-23T00:00:00Z", steps=-5),
        _day("2026-07-21T00:00:00Z", "2026-07-22T00:00:00Z", steps=10_000_000),
    )
    assert normalize_activity_payload(page) == []


def test_boolean_is_not_read_as_a_count() -> None:
    page = _page(_day("2026-07-22T00:00:00Z", "2026-07-23T00:00:00Z", steps=True))
    assert normalize_activity_payload(page) == []


def test_reversed_window_is_skipped() -> None:
    page = _page(_day("2026-07-23T00:00:00Z", "2026-07-22T00:00:00Z", steps=100))
    assert normalize_activity_payload(page) == []


def test_re_run_is_byte_identical() -> None:
    page = _page(_day("2026-07-22T00:00:00Z", "2026-07-23T00:00:00Z", steps=8500))
    a = normalize_activity_payload(page)
    b = normalize_activity_payload(page)
    assert a[0].content_hash == b[0].content_hash


def test_content_hash_changes_with_the_value() -> None:
    a = normalize_activity_payload(
        _page(_day("2026-07-22T00:00:00Z", "2026-07-23T00:00:00Z", steps=8500))
    )
    b = normalize_activity_payload(
        _page(_day("2026-07-22T00:00:00Z", "2026-07-23T00:00:00Z", steps=9000))
    )
    assert a[0].content_hash != b[0].content_hash


def test_malformed_payload_raises() -> None:
    with pytest.raises(NormalizationError):
        normalize_activity_payload("not json")
    with pytest.raises(NormalizationError):
        normalize_activity_payload(json.dumps({"no": "dataPoints"}))
