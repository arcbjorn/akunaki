"""Tests for the Polar workout normalizer (v0.1.0)."""

from __future__ import annotations

import json

import pytest

from akunaki.domain.workout_normalizer import (
    ENTITY_TYPE,
    NormalizationError,
    normalize_workout_payload,
)


def _record(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": "ex-1",
        "start_time": "2026-07-22T06:00:00+02:00",
        "duration": "PT1H",
        "heart_rate_zones": [
            {"index": 1, "in_zone": "PT10M"},
            {"index": 2, "in_zone": "PT20M"},
            {"index": 3, "in_zone": "PT30M"},
            {"index": 4, "in_zone": "PT5M"},
            {"index": 5, "in_zone": "PT2M"},
        ],
    }
    values.update(overrides)
    return values


def _page(*records: dict[str, object]) -> str:
    return json.dumps({"data": list(records)})


def test_computes_canonical_load_from_zones() -> None:
    fact = normalize_workout_payload(_page(_record()))[0]
    # 10*1 + 20*2 + 30*3 + 5*4 + 2*5 = 170.
    assert fact.session_load == pytest.approx(170.0)
    assert fact.zone3_min == pytest.approx(30.0)


def test_assigned_to_local_start_date() -> None:
    # 06:00 local on the 22nd -> local health day is the 22nd.
    fact = normalize_workout_payload(_page(_record()))[0]
    assert fact.local_health_day == "2026-07-22"


def test_fact_key_and_entity_type() -> None:
    fact = normalize_workout_payload(_page(_record()))[0]
    assert fact.fact_key == f"{ENTITY_TYPE}:ex-1"


def test_numeric_seconds_zone_durations() -> None:
    record = _record(
        heart_rate_zones={
            "zone1": 600,
            "zone2": 1200,
            "zone3": 1800,
            "zone4": 300,
            "zone5": 120,
        }
    )
    fact = normalize_workout_payload(_page(record))[0]
    assert fact.session_load == pytest.approx(170.0)  # same minutes as the ISO case


def test_incomplete_zones_are_skipped() -> None:
    # Only four zones -> not a usable record.
    record = _record(
        heart_rate_zones=[
            {"index": 1, "in_zone": "PT10M"},
            {"index": 2, "in_zone": "PT20M"},
            {"index": 3, "in_zone": "PT30M"},
            {"index": 4, "in_zone": "PT5M"},
        ]
    )
    assert normalize_workout_payload(_page(record)) == []


def test_missing_zones_are_skipped() -> None:
    record = _record()
    del record["heart_rate_zones"]
    assert normalize_workout_payload(_page(record)) == []


def test_deterministic_and_hash_tracks_load() -> None:
    first = normalize_workout_payload(_page(_record()))
    second = normalize_workout_payload(_page(_record()))
    assert first == second
    heavier = normalize_workout_payload(
        _page(
            _record(
                heart_rate_zones=[
                    {"index": 1, "in_zone": "PT10M"},
                    {"index": 2, "in_zone": "PT20M"},
                    {"index": 3, "in_zone": "PT30M"},
                    {"index": 4, "in_zone": "PT30M"},
                    {"index": 5, "in_zone": "PT2M"},
                ]
            )
        )
    )
    assert first[0].content_hash != heavier[0].content_hash


def test_invalid_json_raises() -> None:
    with pytest.raises(NormalizationError, match="not valid json"):
        normalize_workout_payload("{oops")


def test_no_records_raises() -> None:
    with pytest.raises(NormalizationError, match="no exercise records"):
        normalize_workout_payload(json.dumps({"meta": {}}))


def test_bare_list_payload_is_accepted() -> None:
    facts = normalize_workout_payload(json.dumps([_record()]))
    assert len(facts) == 1
