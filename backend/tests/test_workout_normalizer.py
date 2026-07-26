"""Tests for the Polar workout normalizer (v0.1.0).

Fixtures use the real `exerciseHashId` shape returned by
`GET /v3/exercises?zones=true`: snake_case fields, a naive local `start_time`
with its offset in `start_time_utc_offset`, and hyphenated `in-zone` durations.
"""

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
        # Local wall-clock time, no zone: 06:00 at +02:00 is 04:00Z.
        "start_time": "2026-07-22T06:00:00",
        "start_time_utc_offset": 120,
        "duration": "PT1H",
        "sport": "RUNNING",
        "heart_rate_zones": [
            {"index": 1, "in-zone": "PT10M"},
            {"index": 2, "in-zone": "PT20M"},
            {"index": 3, "in-zone": "PT30M"},
            {"index": 4, "in-zone": "PT5M"},
            {"index": 5, "in-zone": "PT2M"},
        ],
    }
    values.update(overrides)
    return values


def _page(*records: dict[str, object]) -> str:
    """`GET /v3/exercises` answers with a bare array."""
    return json.dumps(list(records))


def test_computes_canonical_load_from_zones() -> None:
    fact = normalize_workout_payload(_page(_record()))[0]
    # 10*1 + 20*2 + 30*3 + 5*4 + 2*5 = 170.
    assert fact.session_load == pytest.approx(170.0)
    assert fact.zone3_min == pytest.approx(30.0)


def test_naive_start_time_is_anchored_by_its_offset() -> None:
    """`start_time` carries no zone; `start_time_utc_offset` supplies it.

    Parsing the timestamp alone would yield a naive datetime, so the instant
    must come from combining the two — otherwise every workout is dropped.
    """
    fact = normalize_workout_payload(_page(_record()))[0]
    assert fact.start_utc == "2026-07-22T04:00:00Z"  # 06:00 local, +120 min
    assert fact.end_utc == "2026-07-22T05:00:00Z"  # +PT1H
    assert fact.source_offset_minutes == 120


def test_assigned_to_local_start_date() -> None:
    # 06:00 local on the 22nd -> local health day is the 22nd, not the UTC day.
    fact = normalize_workout_payload(_page(_record()))[0]
    assert fact.local_health_day == "2026-07-22"


def test_local_day_differs_from_the_utc_day() -> None:
    """A late-evening local workout must not slide onto the next UTC day."""
    record = _record(start_time="2026-07-22T23:30:00", start_time_utc_offset=-300)
    fact = normalize_workout_payload(_page(record))[0]
    # 23:30 at -05:00 is 04:30Z on the 23rd, but the local day is the 22nd.
    assert fact.start_utc == "2026-07-23T04:30:00Z"
    assert fact.local_health_day == "2026-07-22"


def test_missing_offset_is_skipped() -> None:
    """Without an offset the instant is unknowable; guessing would misdate it."""
    record = _record()
    del record["start_time_utc_offset"]
    assert normalize_workout_payload(_page(record)) == []


def test_implausible_offset_is_skipped() -> None:
    assert normalize_workout_payload(_page(_record(start_time_utc_offset=99999))) == []


def test_fact_key_and_entity_type() -> None:
    fact = normalize_workout_payload(_page(_record()))[0]
    assert fact.fact_key == f"{ENTITY_TYPE}:ex-1"


def test_numeric_seconds_zone_durations() -> None:
    # AccessLink documents ISO-8601 `in-zone`, but the parser also accepts a
    # numeric seconds value, so a vendor sending raw seconds still normalizes.
    record = _record(
        heart_rate_zones=[
            {"index": 1, "in-zone": 600},
            {"index": 2, "in-zone": 1200},
            {"index": 3, "in-zone": 1800},
            {"index": 4, "in-zone": 300},
            {"index": 5, "in-zone": 120},
        ]
    )
    fact = normalize_workout_payload(_page(record))[0]
    assert fact.session_load == pytest.approx(170.0)  # same minutes as the ISO case


def test_incomplete_zones_are_skipped() -> None:
    # Only four zones -> not a usable record.
    record = _record(
        heart_rate_zones=[
            {"index": 1, "in-zone": "PT10M"},
            {"index": 2, "in-zone": "PT20M"},
            {"index": 3, "in-zone": "PT30M"},
            {"index": 4, "in-zone": "PT5M"},
        ]
    )
    assert normalize_workout_payload(_page(record)) == []


def test_missing_zones_are_skipped() -> None:
    """Without `zones=true` the exercises carry no zone data and yield nothing."""
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
                    {"index": 1, "in-zone": "PT10M"},
                    {"index": 2, "in-zone": "PT20M"},
                    {"index": 3, "in-zone": "PT30M"},
                    {"index": 4, "in-zone": "PT30M"},
                    {"index": 5, "in-zone": "PT2M"},
                ]
            )
        )
    )
    assert first[0].content_hash != heavier[0].content_hash


def test_invalid_json_raises() -> None:
    with pytest.raises(NormalizationError, match="not valid json"):
        normalize_workout_payload("{oops")


def test_no_records_raises() -> None:
    with pytest.raises(NormalizationError, match="exercise object or an exercises array"):
        normalize_workout_payload(json.dumps({"meta": {}}))


def test_single_record_slice_is_accepted() -> None:
    """`split_page` stores one exercise per revision, so a bare object parses.

    This is the shape the normalize handler actually passes in production.
    """
    [fact] = normalize_workout_payload(json.dumps(_record()))
    assert fact.session_load == pytest.approx(170.0)


def test_transactional_field_forms_are_not_accepted() -> None:
    """`start-time`/`in_zone` belong to the deprecated transaction resource.

    This connector reads `GET /v3/exercises`, whose schema is snake_case with
    hyphenated zone fields. Accepting the deprecated spelling would mean
    carrying a shape the client never receives.
    """
    record = {
        "id": "ex-hyphen",
        "start-time": "2026-07-22T06:00:00.000",
        "duration": "PT1H",
        "heart_rate_zones": [{"index": i, "in_zone": "PT10M"} for i in range(1, 6)],
    }
    with pytest.raises(NormalizationError, match="exercise object or an exercises array"):
        normalize_workout_payload(json.dumps(record))
