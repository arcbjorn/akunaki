"""Golden tests for the Google Health daily-activity normalizer."""

from __future__ import annotations

import json

import pytest

from akunaki.domain.activity_normalizer import (
    NORMALIZER_VERSION,
    normalize_activity_payload,
    normalize_oura_activity_payload,
    normalize_polar_activity_payload,
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
    # Identity is namespaced by provider so two providers covering one day
    # compete for source selection instead of superseding each other.
    assert fact.vendor_record_id == "activity:google_health:2026-07-22"
    assert fact.fact_key == "daily_activity:activity:google_health:2026-07-22"


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


def test_zero_steps_is_a_valid_confirmed_rest() -> None:
    # Zero is a real value (a rest day), not a dropped signal.
    page = _page(_day("2026-07-22T00:00:00Z", "2026-07-23T00:00:00Z", steps=0))
    facts = normalize_activity_payload(page)
    assert len(facts) == 1
    assert facts[0].steps == 0


def test_steps_bound_is_inclusive() -> None:
    # Exactly the sanity cap (200_000) is kept; one past it is dropped.
    at_cap = _page(_day("2026-07-22T00:00:00Z", "2026-07-23T00:00:00Z", steps=200_000))
    over_cap = _page(_day("2026-07-22T00:00:00Z", "2026-07-23T00:00:00Z", steps=200_001))
    assert normalize_activity_payload(at_cap)[0].steps == 200_000
    assert normalize_activity_payload(over_cap) == []


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


# ---------------------------------------------------------------------------
# Oura daily activity
# ---------------------------------------------------------------------------


def test_oura_reads_the_vendor_day_and_sums_moderate_plus_minutes() -> None:
    """Oura reports `day` directly and activity time in **seconds**."""
    payload = json.dumps(
        {
            "data": [
                {
                    "day": "2026-08-10",
                    "timestamp": "2026-08-10T04:00:00.000-03:00",
                    "steps": 3421,
                    "medium_activity_time": 1800,  # 30 min
                    "high_activity_time": 600,  # 10 min
                    "low_activity_time": 9999,  # excluded: below moderate
                    "sedentary_time": 9999,  # excluded
                }
            ]
        }
    )

    [fact] = normalize_oura_activity_payload(payload)

    assert fact.local_health_day == "2026-08-10"
    assert fact.steps == 3421
    # 30 + 10 minutes; low/sedentary time is deliberately not counted.
    assert fact.active_minutes == 40.0
    assert fact.source_offset_minutes == -180


def test_oura_record_without_any_signal_is_skipped() -> None:
    """The detail table forbids a row carrying neither signal."""
    payload = json.dumps({"data": [{"day": "2026-08-10", "timestamp": "2026-08-10T04:00:00Z"}]})

    assert normalize_oura_activity_payload(payload) == []


def test_oura_record_without_a_day_is_skipped_not_derived() -> None:
    """The vendor already resolved the day boundary; never re-derive it."""
    payload = json.dumps({"data": [{"timestamp": "2026-08-10T04:00:00Z", "steps": 100}]})

    assert normalize_oura_activity_payload(payload) == []


def test_oura_malformed_payload_raises() -> None:
    with pytest.raises(NormalizationError):
        normalize_oura_activity_payload("not json")


# ---------------------------------------------------------------------------
# Polar daily activity
# ---------------------------------------------------------------------------


def test_polar_aggregates_several_records_into_one_day() -> None:
    """Polar emits a new summary as the day progresses.

    Taking the last record would discard the earlier part of the day, so the
    records for a local day are summed.
    """
    payload = json.dumps(
        [
            {
                "start_time": "2026-08-12T08:00:00",
                "end_time": "2026-08-12T12:00:00",
                "steps": 1000,
                "active_duration": "PT30M",
            },
            {
                "start_time": "2026-08-12T12:00:00",
                "end_time": "2026-08-12T23:59:59",
                "steps": 2500,
                "active_duration": "PT1H15M30S",
            },
        ]
    )

    [fact] = normalize_polar_activity_payload(payload)

    assert fact.local_health_day == "2026-08-12"
    assert fact.steps == 3500
    # 30 + 75.5 minutes.
    assert fact.active_minutes == 105.5
    # Polar's timestamps are naive local time; no offset is claimed.
    assert fact.source_offset_minutes is None


def test_polar_separate_days_stay_separate() -> None:
    payload = json.dumps(
        [
            {"start_time": "2026-08-12T08:00:00", "steps": 100, "active_duration": "PT10M"},
            {"start_time": "2026-08-13T08:00:00", "steps": 200, "active_duration": "PT20M"},
        ]
    )

    facts = normalize_polar_activity_payload(payload)

    assert [(f.local_health_day, f.steps) for f in facts] == [
        ("2026-08-12", 100),
        ("2026-08-13", 200),
    ]


def test_polar_duration_parsing_handles_hours_minutes_seconds() -> None:
    payload = json.dumps(
        [{"start_time": "2026-08-12T08:00:00", "active_duration": "PT2H3M30S", "steps": 1}]
    )

    [fact] = normalize_polar_activity_payload(payload)

    assert fact.active_minutes == 123.5


def test_polar_unparseable_duration_does_not_invent_minutes() -> None:
    """A duration we cannot read must not become a number."""
    payload = json.dumps(
        [{"start_time": "2026-08-12T08:00:00", "active_duration": "garbage", "steps": 7}]
    )

    [fact] = normalize_polar_activity_payload(payload)

    assert fact.steps == 7
    assert fact.active_minutes is None


def test_polar_malformed_payload_raises() -> None:
    with pytest.raises(NormalizationError):
        normalize_polar_activity_payload('{"not": "an array"}')


def test_providers_share_a_day_but_never_a_fact_key() -> None:
    """Two providers covering one day must remain two competing facts.

    A daily aggregate has no vendor-assigned id, so keying on the day alone made
    both providers the same logical fact: the second to sync superseded the
    first as a new version, whichever synced last silently won, and the source
    policy never saw a contest. Identity is therefore namespaced by provider.
    """
    oura = normalize_oura_activity_payload(
        json.dumps({"data": [{"day": "2026-08-12", "steps": 10, "high_activity_time": 60}]})
    )
    polar = normalize_polar_activity_payload(
        json.dumps([{"start_time": "2026-08-12T08:00:00", "steps": 20, "active_duration": "PT5M"}])
    )

    # Same day — that is what makes them competitors for source selection.
    assert oura[0].local_health_day == polar[0].local_health_day
    # Distinct identity — so neither supersedes the other.
    assert oura[0].fact_key != polar[0].fact_key
    assert "oura" in oura[0].fact_key
    assert "polar" in polar[0].fact_key
