"""Golden tests for canonical zone-load (v0.1.0).

The weighted-sum formula is a stability contract; values are computed by hand
from health-engine.md.
"""

from __future__ import annotations

import pytest

from akunaki.domain.zone_load import (
    DEFAULT_ZONE_WEIGHTS,
    ZoneMinutes,
    daily_strain_load,
    session_load,
)


def test_session_load_is_weighted_sum() -> None:
    # 10*1 + 20*2 + 30*3 + 5*4 + 2*5 = 10 + 40 + 90 + 20 + 10 = 170.
    zones = ZoneMinutes(z1=10, z2=20, z3=30, z4=5, z5=2)
    assert session_load(zones) == pytest.approx(170.0)


def test_all_zero_session_is_zero_load() -> None:
    assert session_load(ZoneMinutes(z1=0, z2=0, z3=0, z4=0, z5=0)) == 0.0


def test_higher_zones_weigh_more() -> None:
    # The same minutes in Z5 outweigh Z1 fivefold.
    z1_only = session_load(ZoneMinutes(z1=10, z2=0, z3=0, z4=0, z5=0))
    z5_only = session_load(ZoneMinutes(z1=0, z2=0, z3=0, z4=0, z5=10))
    assert z5_only == pytest.approx(5 * z1_only)


def test_default_weights_are_1_through_5() -> None:
    assert DEFAULT_ZONE_WEIGHTS == (1.0, 2.0, 3.0, 4.0, 5.0)


def test_custom_weights() -> None:
    zones = ZoneMinutes(z1=1, z2=1, z3=1, z4=1, z5=1)
    assert session_load(zones, weights=(2.0, 2.0, 2.0, 2.0, 2.0)) == pytest.approx(10.0)


def test_negative_zone_minutes_raise() -> None:
    with pytest.raises(ValueError, match="zone minutes must be non-negative"):
        session_load(ZoneMinutes(z1=-1, z2=0, z3=0, z4=0, z5=0))


def test_daily_load_sums_sessions() -> None:
    assert daily_strain_load([170.0, 30.0, 5.0]) == pytest.approx(205.0)


def test_empty_day_is_confirmed_rest_zero() -> None:
    # An empty list is a confirmed rest with coverage -> known 0.
    assert daily_strain_load([]) == 0.0


def test_negative_session_load_raises() -> None:
    with pytest.raises(ValueError, match="session loads must be non-negative"):
        daily_strain_load([100.0, -5.0])
