"""Prior-load balance: descriptive ACWR and its recovery component (v0.1.0).

Pure: no I/O, no clock. These are the exact v0.1.0 formulas from
health-engine.md. ACWR here is **descriptive only** — never injury prediction
or causation, and UI copy must not imply otherwise.

ACWR = acute load / chronic weekly equivalent, under **strict** coverage: every
day of the 7-day acute window and the 28-day chronic window must be known
(confirmed rest counts as a known zero; missing data is never zero). When any
required day is unknown, ACWR is undefined and the prior-load component is
omitted from recovery — never invented at a midpoint.

The band centers on 1.0: 0.8-1.3 is the descriptive balance band (c = 100),
under-load pulls the score down toward 0, and over-load past 1.3 pulls it down
to 0 at a >= 2.0.
"""

from __future__ import annotations

from dataclasses import dataclass

ACUTE_WINDOW_DAYS = 7
CHRONIC_WINDOW_DAYS = 28

# Band edges (exact v0.1.0).
_BALANCE_LOW = 0.8
_BALANCE_HIGH = 1.3
# Over-load reaches c = 0 at this ACWR.
_OVERLOAD_ZERO = 2.0


# Sentinel reason: chronic and acute both zero over fully-known rest.
ALL_ZERO_REST = "all_zero_rest"


@dataclass(frozen=True, slots=True)
class AcwrResult:
    """A windowed ACWR computation.

    ``acwr`` is None when undefined (insufficient coverage, or a zero chronic
    denominator with nonzero acute). ``defined`` distinguishes a genuine null
    (undefined) from the ``all_zero_rest`` case, which is *defined as balanced*
    with a null stored ACWR.
    """

    acwr: float | None
    defined: bool
    acute_load: float | None
    chronic_weekly_equivalent: float | None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class PriorLoadComponent:
    """The prior-load balance component score, or its absence."""

    present: bool
    c: float | None


def compute_acwr(
    *,
    acute_daily_loads: list[float | None],
    chronic_daily_loads: list[float | None],
) -> AcwrResult:
    """Descriptive ACWR under strict 7/7 and 28/28 coverage.

    Each list holds one daily strain-load per day; ``None`` marks an unknown
    day (confirmed rest is a known ``0.0``, never ``None``). The lists must be
    exactly 7 and 28 long respectively.
    """
    if len(acute_daily_loads) != ACUTE_WINDOW_DAYS:
        msg = f"acute window must be {ACUTE_WINDOW_DAYS} days"
        raise ValueError(msg)
    if len(chronic_daily_loads) != CHRONIC_WINDOW_DAYS:
        msg = f"chronic window must be {CHRONIC_WINDOW_DAYS} days"
        raise ValueError(msg)

    # Strict coverage: any unknown day makes ACWR undefined.
    if any(v is None for v in acute_daily_loads) or any(v is None for v in chronic_daily_loads):
        return AcwrResult(
            acwr=None,
            defined=False,
            acute_load=None,
            chronic_weekly_equivalent=None,
            reason="insufficient_coverage",
        )

    acute_load = sum(v for v in acute_daily_loads if v is not None)
    chronic_weekly_equivalent = sum(v for v in chronic_daily_loads if v is not None) / 4.0

    if chronic_weekly_equivalent == 0.0:
        if acute_load == 0.0:
            # Full known rest across both windows: defined as balanced, null ACWR.
            return AcwrResult(
                acwr=None,
                defined=True,
                acute_load=0.0,
                chronic_weekly_equivalent=0.0,
                reason=ALL_ZERO_REST,
            )
        # Nonzero acute over zero chronic: ACWR undefined.
        return AcwrResult(
            acwr=None,
            defined=False,
            acute_load=acute_load,
            chronic_weekly_equivalent=0.0,
            reason="zero_chronic_denominator",
        )

    return AcwrResult(
        acwr=acute_load / chronic_weekly_equivalent,
        defined=True,
        acute_load=acute_load,
        chronic_weekly_equivalent=chronic_weekly_equivalent,
    )


def prior_load_balance(result: AcwrResult) -> PriorLoadComponent:
    """Map an ACWR result to the prior-load component score, or omit it.

    Omitted (``present=False``) when ACWR is undefined. The ``all_zero_rest``
    case is defined-as-balanced: c = 100 with a null ACWR.
    """
    if not result.defined:
        return PriorLoadComponent(present=False, c=None)
    if result.reason == ALL_ZERO_REST:
        return PriorLoadComponent(present=True, c=100.0)

    assert result.acwr is not None  # defined and not all-zero-rest -> real ACWR
    return PriorLoadComponent(present=True, c=_band_score(result.acwr))


def _band_score(a: float) -> float:
    """The descriptive band mapping (exact v0.1.0)."""
    if a < _BALANCE_LOW:
        # Under-load pulls toward 0 as a -> 0.
        return _clamp(100.0 * (a / _BALANCE_LOW), 0.0, 100.0)
    if a <= _BALANCE_HIGH:
        # Descriptive balance band.
        return 100.0
    # Over-load: c = 0 at a >= 2.0.
    return _clamp(
        100.0 * (1.0 - (a - _BALANCE_HIGH) / (_OVERLOAD_ZERO - _BALANCE_HIGH)), 0.0, 100.0
    )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
