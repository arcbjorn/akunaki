"""Oura sleep normalizer: canonical rules and determinism.

The normalizer is pure, so these need no database. The rules under test are
the ones the engine later depends on: wake-date assignment, canonical units,
and honest quality grading when stage detail is missing.
"""

from __future__ import annotations

import json

import pytest

from akunaki.domain.sleep_normalizer import (
    ENTITY_TYPE,
    NORMALIZER_VERSION,
    NormalizationError,
    normalize_sleep_payload,
)


def _page(*records: dict[str, object]) -> str:
    return json.dumps({"data": list(records)})


def _record(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": "sleep-1",
        # 23:10 local on the 18th → 07:20 local on the 19th (+02:00).
        "bedtime_start": "2026-07-18T23:10:00+02:00",
        "bedtime_end": "2026-07-19T07:20:00+02:00",
        "total_sleep_duration": 27000,  # 450 min
        "time_in_bed": 29400,  # 490 min
        "light_sleep_duration": 15000,
        "deep_sleep_duration": 6000,
        "rem_sleep_duration": 6000,
        "awake_time": 2400,
        "efficiency": 92,
        "type": "long_sleep",
    }
    values.update(overrides)
    return values


# ---------------------------------------------------------------------------
# Canonical assignment rules
# ---------------------------------------------------------------------------


def test_sleep_is_assigned_to_the_wake_date_not_onset() -> None:
    """A night that starts on the 18th and ends on the 19th belongs to the 19th."""
    [fact] = normalize_sleep_payload(_page(_record()))

    assert fact.local_health_day == "2026-07-19"
    assert fact.start_utc.startswith("2026-07-18")


def test_wake_date_uses_local_offset_not_utc() -> None:
    """A 00:30 local wake must not be pushed to the previous UTC day."""
    [fact] = normalize_sleep_payload(
        _page(
            _record(
                bedtime_start="2026-07-18T16:00:00+09:00",
                bedtime_end="2026-07-19T00:30:00+09:00",
            )
        )
    )

    # 00:30+09:00 is 15:30Z on the 18th, but the local wake date is the 19th.
    assert fact.end_utc == "2026-07-18T15:30:00Z"
    assert fact.local_health_day == "2026-07-19"


def test_durations_are_converted_to_canonical_minutes() -> None:
    [fact] = normalize_sleep_payload(_page(_record()))

    # Vendor reports seconds; canonical storage is minutes.
    assert fact.duration_min == 450.0
    assert fact.time_in_bed_min == 490.0
    assert fact.light_min == 250.0
    assert fact.deep_min == 100.0
    assert fact.rem_min == 100.0
    assert fact.awake_min == 40.0


def test_offset_minutes_recorded() -> None:
    [fact] = normalize_sleep_payload(_page(_record()))
    assert fact.source_offset_minutes == 120


def test_fact_key_is_stable_for_a_vendor_record() -> None:
    [first] = normalize_sleep_payload(_page(_record()))
    [second] = normalize_sleep_payload(_page(_record(efficiency=80)))

    # Same logical session across value changes: the key must not move.
    assert first.fact_key == second.fact_key == f"{ENTITY_TYPE}:sleep-1"
    assert first.content_hash != second.content_hash


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_normalization_is_deterministic() -> None:
    """Re-normalizing the same payload must produce identical facts."""
    payload = _page(_record())
    first = normalize_sleep_payload(payload)
    second = normalize_sleep_payload(payload)

    assert first == second
    assert first[0].content_hash == second[0].content_hash


def test_content_hash_ignores_key_order_in_payload() -> None:
    ordered = json.dumps({"data": [_record()]}, sort_keys=True)
    shuffled = json.dumps({"data": [dict(reversed(list(_record().items())))]})

    [a] = normalize_sleep_payload(ordered)
    [b] = normalize_sleep_payload(shuffled)
    assert a.content_hash == b.content_hash


def test_content_hash_changes_when_a_value_changes() -> None:
    [base] = normalize_sleep_payload(_page(_record()))
    [changed] = normalize_sleep_payload(_page(_record(total_sleep_duration=28800)))

    assert base.content_hash != changed.content_hash
    assert changed.duration_min == 480.0


# ---------------------------------------------------------------------------
# Quality grading
# ---------------------------------------------------------------------------


def test_full_record_is_high_quality() -> None:
    [fact] = normalize_sleep_payload(_page(_record()))
    assert fact.quality == "high"
    assert fact.confidence == pytest.approx(0.95)


def test_missing_stages_lowers_quality() -> None:
    """A night without stage detail must not present as complete."""
    [fact] = normalize_sleep_payload(
        _page(
            _record(
                light_sleep_duration=None,
                deep_sleep_duration=None,
                rem_sleep_duration=None,
            )
        )
    )

    assert fact.quality == "medium"
    assert fact.light_min is None


def test_missing_total_falls_back_to_bed_interval_at_low_quality() -> None:
    [fact] = normalize_sleep_payload(
        _page(
            _record(
                total_sleep_duration=None,
                light_sleep_duration=None,
                deep_sleep_duration=None,
                rem_sleep_duration=None,
            )
        )
    )

    assert fact.quality == "low"
    # 23:10 -> 07:20 is 490 minutes of bed interval.
    assert fact.duration_min == 490.0


# ---------------------------------------------------------------------------
# Naps and multiple sessions
# ---------------------------------------------------------------------------


def test_vendor_nap_type_is_honored() -> None:
    [fact] = normalize_sleep_payload(_page(_record(type="nap")))
    assert fact.is_nap is True


def test_multiple_sessions_per_day_are_all_kept() -> None:
    """Split sleep and naps are separate facts, never merged."""
    facts = normalize_sleep_payload(
        _page(
            _record(id="main"),
            _record(
                id="nap-1",
                type="nap",
                bedtime_start="2026-07-19T14:00:00+02:00",
                bedtime_end="2026-07-19T14:45:00+02:00",
                total_sleep_duration=2400,
            ),
        )
    )

    assert len(facts) == 2
    assert {f.vendor_record_id for f in facts} == {"main", "nap-1"}
    assert all(f.local_health_day == "2026-07-19" for f in facts)


# ---------------------------------------------------------------------------
# Malformed input
# ---------------------------------------------------------------------------


def test_invalid_json_raises() -> None:
    with pytest.raises(NormalizationError, match="not valid json"):
        normalize_sleep_payload("<html>")


def test_missing_data_array_raises() -> None:
    with pytest.raises(NormalizationError, match="no data array"):
        normalize_sleep_payload(json.dumps({"next_token": None}))


def test_empty_page_yields_no_facts() -> None:
    assert normalize_sleep_payload(_page()) == []


@pytest.mark.parametrize(
    "broken",
    [
        {"bedtime_start": "2026-07-18T23:00:00+02:00"},  # no id
        {"id": "x"},  # no timestamps
        {"id": "x", "bedtime_start": "nonsense", "bedtime_end": "nonsense"},
    ],
)
def test_unusable_records_are_skipped_not_fatal(broken: dict[str, object]) -> None:
    """One bad record must not block a whole night's ingestion."""
    facts = normalize_sleep_payload(_page(broken, _record(id="good")))

    assert len(facts) == 1
    assert facts[0].vendor_record_id == "good"


def test_inverted_interval_is_skipped() -> None:
    facts = normalize_sleep_payload(
        _page(
            _record(
                id="bad",
                bedtime_start="2026-07-19T07:00:00+02:00",
                bedtime_end="2026-07-18T23:00:00+02:00",
            )
        )
    )
    assert facts == []


def test_out_of_range_efficiency_is_dropped_not_stored() -> None:
    [fact] = normalize_sleep_payload(_page(_record(efficiency=180)))
    assert fact.efficiency_pct is None


def test_negative_durations_are_rejected() -> None:
    [fact] = normalize_sleep_payload(_page(_record(deep_sleep_duration=-100)))
    assert fact.deep_min is None


def test_normalizer_version_is_pinned() -> None:
    # Facts record which normalizer produced them; changing it is deliberate.
    assert NORMALIZER_VERSION == "oura_sleep_v0.1.0"
