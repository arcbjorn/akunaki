"""Golden tests for sleep consistency (mean resultant length, v0.1.0).

Every expected value is computed by hand from health-engine.md. R is a
stability contract, so these are deliberately exact.
"""

from __future__ import annotations

import math

import pytest

from akunaki.domain.sleep_consistency import (
    MIN_VALID_NIGHTS,
    midpoint_local_minutes,
    sleep_consistency,
)

# ---------------------------------------------------------------------------
# Midpoint on the clock
# ---------------------------------------------------------------------------


def test_midpoint_is_halfway_through_the_session() -> None:
    # 22:00 (1320) start, 8h (480) duration -> midpoint 02:00 (120) next day.
    assert midpoint_local_minutes(start_local_minutes=1320, duration_minutes=480) == 120.0


def test_midpoint_without_wrap() -> None:
    # 00:00 start, 8h -> 04:00 (240).
    assert midpoint_local_minutes(start_local_minutes=0, duration_minutes=480) == 240.0


def test_midpoint_rejects_negative_duration() -> None:
    with pytest.raises(ValueError, match="duration_minutes must be non-negative"):
        midpoint_local_minutes(start_local_minutes=0, duration_minutes=-1)


# ---------------------------------------------------------------------------
# Resultant length and score
# ---------------------------------------------------------------------------


def test_identical_midpoints_score_100() -> None:
    # Perfect regularity: every night at the same midpoint -> R = 1 -> 100.
    result = sleep_consistency([180.0] * 7)
    assert result.resultant_length == pytest.approx(1.0)
    assert result.score == pytest.approx(100.0)
    assert result.is_usable is True


def test_diametrically_opposed_midpoints_score_0() -> None:
    # Seven nights at 00:00 and seven at 12:00 (720): the vectors cancel -> R=0.
    midpoints = [0.0] * 7 + [720.0] * 7
    result = sleep_consistency(midpoints)
    assert result.resultant_length == pytest.approx(0.0, abs=1e-12)
    assert result.score == pytest.approx(0.0)


def test_score_matches_hand_computed_resultant() -> None:
    # Midpoints at 03:00 (180) and 05:00 (300): 7 each.
    # angle(180) and angle(300); R = |mean of the two unit vectors| since the
    # counts are equal, which is cos(half the angular separation).
    midpoints = [180.0] * 7 + [300.0] * 7
    result = sleep_consistency(midpoints)
    a1 = 2 * math.pi * 180 / 1440
    a2 = 2 * math.pi * 300 / 1440
    mean_cos = (7 * math.cos(a1) + 7 * math.cos(a2)) / 14
    mean_sin = (7 * math.sin(a1) + 7 * math.sin(a2)) / 14
    expected_r = math.sqrt(mean_cos**2 + mean_sin**2)
    assert result.resultant_length == pytest.approx(expected_r)
    assert result.score == pytest.approx(100 * expected_r)


def test_wraparound_midnight_cluster_is_tight() -> None:
    # Midpoints just before and after midnight (23:50 and 00:10) are 20 min
    # apart on the clock, not 1420 — circular stats handle the wrap, so R is
    # very close to 1.
    result = sleep_consistency([1430.0] * 7 + [10.0] * 7)
    assert result.resultant_length is not None
    assert result.resultant_length > 0.999


# ---------------------------------------------------------------------------
# Minimum-nights gate
# ---------------------------------------------------------------------------


def test_below_seven_nights_is_unusable() -> None:
    result = sleep_consistency([180.0] * (MIN_VALID_NIGHTS - 1))
    assert result.valid_nights == 6
    assert result.score is None
    assert result.resultant_length is None
    assert result.is_usable is False


def test_exactly_seven_nights_is_usable() -> None:
    result = sleep_consistency([180.0] * 7)
    assert result.valid_nights == 7
    assert result.is_usable is True


def test_empty_window_is_unusable() -> None:
    result = sleep_consistency([])
    assert result.valid_nights == 0
    assert result.is_usable is False
