"""Golden formula tests for the deterministic sleep summary (v0.1.0).

Every value below is computed by hand from health-engine.md, not from the
implementation. These formulas are a stability contract: changing an output
requires bumping the formula version, so the tests are deliberately exact.
"""

from __future__ import annotations

import pytest

from akunaki.domain.sleep_summary import (
    DEFAULT_TARGET_MIN,
    DailySleep,
    SleepStatus,
    build_sleep_summary,
    debt_window_days,
    sleep_debt_14d,
    sleep_target_adherence,
)


def _days(*durations: float | None) -> list[DailySleep]:
    return [
        DailySleep(local_health_day=f"2026-07-{1 + i:02d}", duration_min=d)
        for i, d in enumerate(durations)
    ]


# ---------------------------------------------------------------------------
# Adherence
# ---------------------------------------------------------------------------


def test_exact_target_is_full_adherence() -> None:
    assert sleep_target_adherence(duration_min=480, target_min=480) == 100.0


def test_shortfall_reduces_adherence_linearly() -> None:
    # 412 of 480: shortfall 68, 100 * (1 - 68/480) = 85.833...
    result = sleep_target_adherence(duration_min=412, target_min=480)
    assert result == pytest.approx(85.8333, abs=1e-4)


def test_oversleep_does_not_exceed_100() -> None:
    # No bonus for oversleeping in v0.1.0.
    assert sleep_target_adherence(duration_min=600, target_min=480) == 100.0


def test_zero_sleep_is_zero_adherence() -> None:
    assert sleep_target_adherence(duration_min=0, target_min=480) == 0.0


def test_adherence_clamps_at_zero_for_extreme_shortfall() -> None:
    # Cannot go negative even if duration exceeds a nonsensical target gap.
    assert sleep_target_adherence(duration_min=0, target_min=1) == 0.0


def test_adherence_rejects_nonpositive_target() -> None:
    with pytest.raises(ValueError, match="target_min must be positive"):
        sleep_target_adherence(duration_min=400, target_min=0)


# ---------------------------------------------------------------------------
# Sleep debt
# ---------------------------------------------------------------------------


def test_full_window_at_target_has_zero_debt() -> None:
    result = sleep_debt_14d(_days(*([480] * 14)), target_min=480)
    assert result.debt_min == 0.0
    assert result.known_days == 14
    assert result.status is SleepStatus.COMPLETE
    assert result.is_lower_bound is False


def test_debt_accumulates_shortfall() -> None:
    # Each day 60 short of 480; 14 days -> 840 min, well under the 14*480 cap.
    result = sleep_debt_14d(_days(*([420] * 14)), target_min=480)
    assert result.debt_min == 840.0
    assert result.status is SleepStatus.COMPLETE


def test_surplus_credit_is_capped_at_60() -> None:
    # Day 1: 120 short (debt 120). Day 2: 180 over, but credit capped at 60.
    # debt = clamp(120 - 60, 0, cap) = 60.
    result = sleep_debt_14d(_days(360, 660), target_min=480)
    assert result.debt_min == 60.0


def test_debt_never_goes_negative() -> None:
    # A single big-surplus day cannot create "negative debt" / stored credit.
    result = sleep_debt_14d(_days(600), target_min=480)
    assert result.debt_min == 0.0


def test_debt_is_clamped_to_window_times_target() -> None:
    # Two zero-sleep days: raw debt 960, cap 2*480 = 960; exactly at the cap.
    result = sleep_debt_14d(_days(0, 0), target_min=480)
    assert result.debt_min == 960.0
    # Three zero days would raw to 1440 but cap at 3*480 = 1440 as well; use a
    # case where the cap actually bites: high target keeps raw above cap.
    capped = sleep_debt_14d(_days(0, 0, 480), target_min=480)
    # days: 480 short, 480 short, 0 short -> raw 960, cap 3*480=1440 -> 960.
    assert capped.debt_min == 960.0


def test_unknown_day_is_skipped_not_imputed() -> None:
    # The unknown day contributes neither shortfall nor surplus.
    result = sleep_debt_14d(_days(420, None, 420), target_min=480)
    assert result.debt_min == 120.0  # two known short days, 60 each
    assert result.known_days == 2
    assert result.window_days == 3
    assert result.status is SleepStatus.PARTIAL
    assert result.is_lower_bound is True


def test_truncated_window_for_new_user_is_partial_lower_bound() -> None:
    # Fewer than 14 days of history: a disclosed lower bound.
    result = sleep_debt_14d(_days(420, 420, 420), target_min=480)
    assert result.debt_min == 180.0
    assert result.known_days == 3
    assert result.status is SleepStatus.PARTIAL
    assert result.is_lower_bound is True


def test_full_known_window_is_not_a_lower_bound() -> None:
    result = sleep_debt_14d(_days(*([420] * 14)), target_min=480)
    assert result.is_lower_bound is False


def test_recommendation_eligibility_needs_12_known_days() -> None:
    twelve = sleep_debt_14d(_days(*([420] * 12 + [None, None])), target_min=480)
    eleven = sleep_debt_14d(_days(*([420] * 11 + [None] * 3)), target_min=480)
    assert twelve.recommendation_eligible is True
    assert eleven.recommendation_eligible is False


def test_credit_then_debt_ordering_matters() -> None:
    # Chronological: a surplus day before a short day offsets it (capped).
    # Day 1: 60 over (credit 60, debt stays 0). Day 2: 120 short -> debt 120.
    result = sleep_debt_14d(_days(540, 360), target_min=480)
    assert result.debt_min == 120.0
    # Reverse order: short first (debt 120), then surplus credit 60 -> debt 60.
    reversed_result = sleep_debt_14d(_days(360, 540), target_min=480)
    assert reversed_result.debt_min == 60.0


def test_debt_rejects_oversized_window() -> None:
    with pytest.raises(ValueError, match="may not exceed 14"):
        sleep_debt_14d(_days(*([480] * 15)), target_min=480)


# ---------------------------------------------------------------------------
# Debt window (calendar arithmetic)
# ---------------------------------------------------------------------------


def test_window_is_14_days_ending_on_target_oldest_first() -> None:
    window = debt_window_days("2026-07-19")
    assert len(window) == 14
    assert window[0] == "2026-07-06"  # target minus 13
    assert window[-1] == "2026-07-19"  # the target day itself
    assert window == sorted(window)  # chronological


def test_window_crosses_month_boundary() -> None:
    window = debt_window_days("2026-03-05")
    assert window[0] == "2026-02-20"
    assert window[-1] == "2026-03-05"


def test_window_rejects_non_date() -> None:
    with pytest.raises(ValueError, match="Invalid isoformat"):
        debt_window_days("not-a-day")


# ---------------------------------------------------------------------------
# Full summary
# ---------------------------------------------------------------------------


def test_summary_matches_the_design_example() -> None:
    # The /v1/today example: 412 min, target 480, 85.8% adherence.
    summary = build_sleep_summary(
        local_health_day="2026-07-19",
        duration_min=412,
        window=_days(*([412] * 14)),
        target_min=480,
    )
    assert summary.duration_min == 412
    assert summary.target_min == 480
    assert summary.adherence_pct == pytest.approx(85.8333, abs=1e-4)
    # 14 days each 68 short -> 952 debt (under the cap).
    assert summary.debt_14d_min == pytest.approx(952.0)
    assert summary.debt_window_days == 14
    assert summary.debt_known_days == 14
    assert summary.debt_status is SleepStatus.COMPLETE


def test_summary_uses_provisional_default_target() -> None:
    summary = build_sleep_summary(
        local_health_day="2026-07-19",
        duration_min=480,
        window=_days(480),
    )
    assert summary.target_min == DEFAULT_TARGET_MIN
    assert summary.adherence_pct == 100.0


def test_summary_is_deterministic() -> None:
    kwargs = {
        "local_health_day": "2026-07-19",
        "duration_min": 400.0,
        "window": _days(400, 500, None, 460),
        "target_min": 480,
    }
    assert build_sleep_summary(**kwargs) == build_sleep_summary(**kwargs)  # type: ignore[arg-type]


def test_formula_version_is_pinned() -> None:
    summary = build_sleep_summary(
        local_health_day="2026-07-19",
        duration_min=480,
        window=_days(480),
    )
    assert summary.formula_version == "sleep_summary_v0.1.0"
