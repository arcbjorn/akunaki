"""Tests for the Oura overnight-vitals normalizer (v0.1.0).

These cover extraction of HRV/RHR from the sleep payload, the wake-date keying
shared with sleep, nap and empty-signal skipping, sanity bounds, and the
deterministic content hash.
"""

from __future__ import annotations

import json

import pytest

from akunaki.domain.vitals_normalizer import (
    ENTITY_TYPE,
    NormalizationError,
    normalize_vitals_payload,
)


def _page(*records: dict[str, object]) -> str:
    return json.dumps({"data": list(records)})


def _record(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": "sleep-1",
        "bedtime_start": "2026-07-18T23:10:00+02:00",
        "bedtime_end": "2026-07-19T07:20:00+02:00",
        "type": "long_sleep",
        "average_hrv": 62.0,
        "lowest_heart_rate": 48.0,
    }
    values.update(overrides)
    return values


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def test_hrv_and_rhr_are_extracted() -> None:
    facts = normalize_vitals_payload(_page(_record()))
    assert len(facts) == 1
    fact = facts[0]
    assert fact.hrv_ms == 62.0
    assert fact.resting_hr_bpm == 48.0
    assert fact.quality == "high"


def test_keyed_to_wake_date_like_sleep() -> None:
    # 23:10 on the 18th -> 07:20 on the 19th (+02:00): wake date is the 19th.
    facts = normalize_vitals_payload(_page(_record()))
    assert facts[0].local_health_day == "2026-07-19"


def test_entity_type_and_fact_key() -> None:
    fact = normalize_vitals_payload(_page(_record()))[0]
    assert fact.fact_key == f"{ENTITY_TYPE}:sleep-1"


def test_only_hrv_present_is_medium_quality() -> None:
    facts = normalize_vitals_payload(_page(_record(lowest_heart_rate=None)))
    assert facts[0].hrv_ms == 62.0
    assert facts[0].resting_hr_bpm is None
    assert facts[0].quality == "medium"


def test_only_rhr_present_is_medium_quality() -> None:
    facts = normalize_vitals_payload(_page(_record(average_hrv=None)))
    assert facts[0].resting_hr_bpm == 48.0
    assert facts[0].hrv_ms is None
    assert facts[0].quality == "medium"


def test_flat_temperature_deviation_is_extracted() -> None:
    facts = normalize_vitals_payload(_page(_record(temperature_deviation=-0.3)))
    assert facts[0].temperature_deviation_c == pytest.approx(-0.3)
    # HRV, RHR, and temp all present -> high quality.
    assert facts[0].quality == "high"


def test_nested_readiness_temperature_is_extracted() -> None:
    facts = normalize_vitals_payload(_page(_record(readiness={"temperature_deviation": 0.42})))
    assert facts[0].temperature_deviation_c == pytest.approx(0.42)


def test_temperature_only_record_is_kept() -> None:
    # No HRV/RHR but a temperature reading: still a valid vitals fact.
    facts = normalize_vitals_payload(
        _page(_record(average_hrv=None, lowest_heart_rate=None, temperature_deviation=-0.5))
    )
    assert len(facts) == 1
    assert facts[0].temperature_deviation_c == pytest.approx(-0.5)
    assert facts[0].hrv_ms is None
    assert facts[0].quality == "medium"  # only one signal present


def test_out_of_range_temperature_is_dropped() -> None:
    facts = normalize_vitals_payload(_page(_record(temperature_deviation=99.0)))
    assert facts[0].temperature_deviation_c is None
    # HRV and RHR remain.
    assert facts[0].hrv_ms == 62.0


# ---------------------------------------------------------------------------
# Skipping
# ---------------------------------------------------------------------------


def test_nap_is_skipped() -> None:
    assert normalize_vitals_payload(_page(_record(type="nap"))) == []


def test_record_with_no_vitals_is_skipped() -> None:
    empty = _record(average_hrv=None, lowest_heart_rate=None)
    assert normalize_vitals_payload(_page(empty)) == []


def test_out_of_range_values_are_dropped() -> None:
    # HRV above the physiologic max and RHR below the min are dropped; if both
    # drop and nothing remains, the record yields no fact.
    facts = normalize_vitals_payload(_page(_record(average_hrv=9999.0, lowest_heart_rate=48.0)))
    assert facts[0].hrv_ms is None
    assert facts[0].resting_hr_bpm == 48.0

    assert normalize_vitals_payload(_page(_record(average_hrv=9999.0, lowest_heart_rate=5.0))) == []


def test_short_bout_without_type_is_treated_as_nap() -> None:
    # A 90-minute bout with no explicit type is a nap by duration -> skipped.
    short = _record(
        bedtime_start="2026-07-19T02:00:00+02:00",
        bedtime_end="2026-07-19T03:30:00+02:00",
    )
    del short["type"]
    assert normalize_vitals_payload(_page(short)) == []


# ---------------------------------------------------------------------------
# Structure and determinism
# ---------------------------------------------------------------------------


def test_deterministic_across_runs() -> None:
    first = normalize_vitals_payload(_page(_record()))
    second = normalize_vitals_payload(_page(_record()))
    assert first == second


def test_content_hash_changes_when_a_value_changes() -> None:
    base = normalize_vitals_payload(_page(_record()))[0]
    changed = normalize_vitals_payload(_page(_record(average_hrv=70.0)))[0]
    assert base.content_hash != changed.content_hash


def test_content_hash_ignores_payload_key_order() -> None:
    ordered = json.dumps({"data": [_record()]}, sort_keys=True)
    shuffled = json.dumps({"data": [dict(reversed(list(_record().items())))]})
    assert (
        normalize_vitals_payload(ordered)[0].content_hash
        == normalize_vitals_payload(shuffled)[0].content_hash
    )


def test_invalid_json_raises() -> None:
    with pytest.raises(NormalizationError, match="not valid json"):
        normalize_vitals_payload("{not json")


def test_payload_without_data_array_raises() -> None:
    with pytest.raises(NormalizationError, match="no data array"):
        normalize_vitals_payload(json.dumps({"meta": {}}))


def test_single_record_slice_is_accepted() -> None:
    # A per-record slice from the raw layer, not a collection page.
    slice_text = json.dumps(_record())
    facts = normalize_vitals_payload(slice_text)
    assert len(facts) == 1
