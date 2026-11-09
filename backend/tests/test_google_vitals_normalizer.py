"""Tests for the Google Health daily heart-summary (vitals) normalizer."""

from __future__ import annotations

import json

import pytest

from akunaki.domain.google_vitals_normalizer import (
    normalize_google_vitals_payload,
    vendor_record_id_for,
)
from akunaki.domain.sleep_normalizer import NormalizationError
from akunaki.domain.vitals_normalizer import ENTITY_TYPE


def _date(year: int, month: int, day: int) -> dict[str, int]:
    return {"year": year, "month": month, "day": day}


def _rhr(bpm: object, *, day: int = 7) -> dict[str, object]:
    return {
        "dailyRestingHeartRate": {
            "date": _date(2026, 11, day),
            "dailyRestingHeartRateMetadata": {"calculationMethod": "WITH_SLEEP"},
            "beatsPerMinute": bpm,
        }
    }


def _hrv(**fields: object) -> dict[str, object]:
    return {"dailyHeartRateVariability": {"date": _date(2026, 11, 7), **fields}}


def _page(*points: dict[str, object]) -> str:
    return json.dumps({"dataPoints": list(points)})


def test_resting_hr_and_hrv_for_one_day_make_one_fact() -> None:
    [fact] = normalize_google_vitals_payload(
        _page(_rhr("52"), _hrv(averageHeartRateVariabilityMilliseconds=41.5))
    )

    assert fact.local_health_day == "2026-11-07"
    assert fact.vendor_record_id == vendor_record_id_for("2026-11-07")
    assert fact.fact_key == f"{ENTITY_TYPE}:google_vitals:2026-11-07"
    assert (fact.resting_hr_bpm, fact.hrv_ms) == (52.0, 41.5)
    # Two signals grade high, like an Oura night carrying both.
    assert (fact.quality, fact.confidence) == ("high", 0.95)
    # The civil day, expressed as if UTC: the summary carries no offset.
    assert (fact.start_utc, fact.end_utc) == ("2026-11-07T00:00:00Z", "2026-11-08T00:00:00Z")
    assert fact.source_offset_minutes is None


def test_a_single_stream_page_makes_a_half_fact() -> None:
    """Resting HR and HRV arrive on separate streams; each page carries one half."""
    [fact] = normalize_google_vitals_payload(_page(_rhr(48)))

    assert fact.resting_hr_bpm == 48.0
    assert fact.hrv_ms is None
    assert fact.quality == "medium"


def test_deep_sleep_rmssd_stands_in_for_a_missing_average() -> None:
    [fact] = normalize_google_vitals_payload(
        _page(_hrv(deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds=38.0))
    )
    assert fact.hrv_ms == 38.0


def test_days_are_separate_facts_in_date_order() -> None:
    facts = normalize_google_vitals_payload(_page(_rhr(50, day=8), _rhr(49, day=6)))
    assert [f.local_health_day for f in facts] == ["2026-11-06", "2026-11-08"]


def test_out_of_range_and_unparseable_values_are_dropped() -> None:
    # 900 ms HRV is not physiologic; "fast" is not a number; a bool is not a bpm.
    facts = normalize_google_vitals_payload(
        _page(
            _hrv(averageHeartRateVariabilityMilliseconds=900.0),
            _rhr("fast"),
            _rhr(True),
        )
    )
    assert facts == []


def test_a_point_without_a_usable_date_is_skipped() -> None:
    bad: dict[str, object] = {
        "dailyRestingHeartRate": {
            "date": {"year": 2026, "month": 13, "day": 1},
            "beatsPerMinute": "50",
        }
    }
    assert normalize_google_vitals_payload(_page(bad, _rhr(50))) != []
    assert len(normalize_google_vitals_payload(_page(bad, _rhr(50)))) == 1


def test_same_input_hashes_the_same() -> None:
    page = _page(_rhr(52), _hrv(averageHeartRateVariabilityMilliseconds=41.5))
    [first] = normalize_google_vitals_payload(page)
    [second] = normalize_google_vitals_payload(page)
    assert first.content_hash == second.content_hash
    assert first.content_hash


@pytest.mark.parametrize("body", ["not json", "[]", json.dumps({"rollupDataPoints": []})])
def test_structurally_unusable_payloads_raise(body: str) -> None:
    with pytest.raises(NormalizationError):
        normalize_google_vitals_payload(body)
