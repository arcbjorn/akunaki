"""Golden tests for descriptive ACWR and prior-load balance (v0.1.0).

Every expected value is computed by hand from health-engine.md. The band
mapping and coverage rules are a stability contract, so these are exact.
"""

from __future__ import annotations

import pytest

from akunaki.domain.prior_load import (
    ALL_ZERO_REST,
    compute_acwr,
    prior_load_balance,
)


def _acute(*, total: float) -> list[float | None]:
    """Seven known days summing to ``total`` (spread onto the first day)."""
    days: list[float | None] = [total, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    return days


def _chronic(*, total: float) -> list[float | None]:
    """Twenty-eight known days summing to ``total``."""
    days: list[float | None] = [total, *([0.0] * 27)]
    return days


# ---------------------------------------------------------------------------
# ACWR computation and coverage
# ---------------------------------------------------------------------------


def test_acwr_is_acute_over_chronic_weekly() -> None:
    # acute 700, chronic sum 2800 -> weekly equiv 700 -> ACWR 1.0.
    result = compute_acwr(
        acute_daily_loads=_acute(total=700.0),
        chronic_daily_loads=_chronic(total=2800.0),
    )
    assert result.defined is True
    assert result.acute_load == 700.0
    assert result.chronic_weekly_equivalent == pytest.approx(700.0)
    assert result.acwr == pytest.approx(1.0)


def test_unknown_acute_day_makes_acwr_undefined() -> None:
    acute = _acute(total=700.0)
    acute[3] = None
    result = compute_acwr(
        acute_daily_loads=acute,
        chronic_daily_loads=_chronic(total=2800.0),
    )
    assert result.defined is False
    assert result.acwr is None


def test_unknown_chronic_day_makes_acwr_undefined() -> None:
    chronic = _chronic(total=2800.0)
    chronic[10] = None
    result = compute_acwr(
        acute_daily_loads=_acute(total=700.0),
        chronic_daily_loads=chronic,
    )
    assert result.defined is False
    assert result.acwr is None


def test_wrong_window_lengths_raise() -> None:
    with pytest.raises(ValueError, match="acute window must be 7"):
        compute_acwr(acute_daily_loads=[0.0] * 6, chronic_daily_loads=_chronic(total=0.0))
    with pytest.raises(ValueError, match="chronic window must be 28"):
        compute_acwr(acute_daily_loads=_acute(total=0.0), chronic_daily_loads=[0.0] * 27)


# ---------------------------------------------------------------------------
# Zero-denominator special cases
# ---------------------------------------------------------------------------


def test_all_zero_rest_is_defined_balanced() -> None:
    result = compute_acwr(
        acute_daily_loads=_acute(total=0.0),
        chronic_daily_loads=_chronic(total=0.0),
    )
    assert result.defined is True
    assert result.acwr is None  # null stored ACWR
    assert result.reason == ALL_ZERO_REST


def test_zero_chronic_nonzero_acute_is_undefined() -> None:
    result = compute_acwr(
        acute_daily_loads=_acute(total=100.0),
        chronic_daily_loads=_chronic(total=0.0),
    )
    assert result.defined is False
    assert result.acwr is None


# ---------------------------------------------------------------------------
# Band mapping
# ---------------------------------------------------------------------------


def _acwr_of(a: float):
    # Build known windows whose ratio is exactly ``a``: chronic weekly 100,
    # acute 100*a.
    return compute_acwr(
        acute_daily_loads=_acute(total=100.0 * a),
        chronic_daily_loads=_chronic(total=400.0),
    )


def test_balance_band_scores_100() -> None:
    for a in (0.8, 1.0, 1.15, 1.3):
        component = prior_load_balance(_acwr_of(a))
        assert component.present is True
        assert component.c == pytest.approx(100.0), f"a={a}"


def test_under_load_pulls_toward_zero() -> None:
    # a = 0.4: c = 100 * (0.4 / 0.8) = 50.
    assert prior_load_balance(_acwr_of(0.4)).c == pytest.approx(50.0)
    # a -> 0: c -> 0.
    assert prior_load_balance(_acwr_of(0.0)).c == pytest.approx(0.0)


def test_over_load_pulls_toward_zero() -> None:
    # a = 1.65 is halfway from 1.3 to 2.0: c = 100 * (1 - 0.35/0.7) = 50.
    assert prior_load_balance(_acwr_of(1.65)).c == pytest.approx(50.0)
    # a = 2.0: c = 0.
    assert prior_load_balance(_acwr_of(2.0)).c == pytest.approx(0.0)
    # a well past 2.0 stays clamped at 0.
    assert prior_load_balance(_acwr_of(3.0)).c == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Component presence
# ---------------------------------------------------------------------------


def test_undefined_acwr_omits_the_component() -> None:
    acute = _acute(total=700.0)
    acute[0] = None
    component = prior_load_balance(
        compute_acwr(acute_daily_loads=acute, chronic_daily_loads=_chronic(total=2800.0))
    )
    assert component.present is False
    assert component.c is None


def test_all_zero_rest_component_is_balanced() -> None:
    component = prior_load_balance(
        compute_acwr(
            acute_daily_loads=_acute(total=0.0),
            chronic_daily_loads=_chronic(total=0.0),
        )
    )
    assert component.present is True
    assert component.c == pytest.approx(100.0)
