"""Deterministic sleep summary: adherence and 14-day sleep debt.

Pure: no I/O, no clock. Timestamps come from the caller. These are the exact
v0.1.0 formulas from health-engine.md, not a "sleep score" — the design is
explicit that sleep ships as a deterministic summary, and clients must not
imply a score exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

FORMULA_VERSION = "sleep_summary_v0.1.0"

# Provisional default target when the user has set none. Explicitly provisional
# per the design — never a chronically short personal median.
DEFAULT_TARGET_MIN = 480

# Per-day surplus credit cap and the debt window length.
_MAX_DAILY_CREDIT_MIN = 60.0
DEBT_WINDOW_DAYS = 14

# Debt-related recommendations require this many known days in the window.
DEBT_RECOMMENDATION_MIN_KNOWN = 12


class SleepStatus(StrEnum):
    """Coverage status of a windowed sleep computation."""

    COMPLETE = "complete"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class DailySleep:
    """One local day's authoritative sleep duration.

    ``duration_min`` is None when no authoritative sleep is known for the day;
    the debt algorithm skips such days rather than imputing zero.
    """

    local_health_day: str
    duration_min: float | None


@dataclass(frozen=True, slots=True)
class SleepDebtResult:
    """Rolling 14-day sleep debt for a target day."""

    debt_min: float
    known_days: int
    window_days: int
    status: SleepStatus
    is_lower_bound: bool
    """True when the window is not 14/14 known, so actual debt could be higher."""

    @property
    def recommendation_eligible(self) -> bool:
        """Whether debt recommendations may be emitted for this window."""
        return self.known_days >= DEBT_RECOMMENDATION_MIN_KNOWN


def debt_window_days(target_day: str) -> list[str]:
    """The 14 local days of the debt window, oldest-first.

    The window is the target local health day and the previous 13 calendar
    days, as ``YYYY-MM-DD`` strings. This is pure calendar arithmetic on the
    local health day; no timezone or clock is involved.
    """
    anchor = date.fromisoformat(target_day)
    days = [anchor - timedelta(days=offset) for offset in range(DEBT_WINDOW_DAYS)]
    return [day.isoformat() for day in reversed(days)]


def sleep_target_adherence(*, duration_min: float, target_min: int) -> float:
    """Bounded 0-100 adherence versus an explicit sleep target.

    Oversleep does not push adherence above 100 in v0.1.0; surplus is handled
    by debt credit, not adherence.
    """
    if target_min <= 0:
        msg = "target_min must be positive"
        raise ValueError(msg)
    shortfall = max(0.0, target_min - duration_min)
    ratio = 1.0 - shortfall / target_min
    return _clamp(100.0 * ratio, 0.0, 100.0)


def sleep_debt_14d(
    window: list[DailySleep],
    *,
    target_min: int,
) -> SleepDebtResult:
    """Rolling accumulated sleep debt over the day window (chronological).

    Days must be supplied oldest-first and represent the target day plus the
    preceding calendar days (up to 14). An unknown day is skipped, not imputed,
    and marks the window partial. Debt is clamped to ``[0, window * target]``.
    New users with a truncated series compute over available days with the same
    caps.
    """
    if target_min <= 0:
        msg = "target_min must be positive"
        raise ValueError(msg)
    if len(window) > DEBT_WINDOW_DAYS:
        msg = f"window may not exceed {DEBT_WINDOW_DAYS} days"
        raise ValueError(msg)

    debt = 0.0
    known_days = 0
    upper = float(len(window) * target_min)
    for day in window:
        if day.duration_min is None:
            continue
        known_days += 1
        shortfall = max(0.0, target_min - day.duration_min)
        surplus = max(0.0, day.duration_min - target_min)
        credit = min(surplus, _MAX_DAILY_CREDIT_MIN)
        debt = _clamp(debt + shortfall - credit, 0.0, upper)

    # Complete only when all 14 days are present and known (health-engine.md:
    # "status complete if 14/14 known, else partial"; new users with a
    # truncated series are also partial). Both an interior unknown day and a
    # truncated window make the debt a disclosed lower bound: hidden days could
    # only add shortfall, never reduce debt.
    is_complete = known_days == DEBT_WINDOW_DAYS
    return SleepDebtResult(
        debt_min=debt,
        known_days=known_days,
        window_days=len(window),
        status=SleepStatus.COMPLETE if is_complete else SleepStatus.PARTIAL,
        is_lower_bound=not is_complete,
    )


@dataclass(frozen=True, slots=True)
class SleepSummary:
    """The deterministic sleep view for one day."""

    local_health_day: str
    duration_min: float
    target_min: int
    adherence_pct: float
    debt_14d_min: float
    debt_known_days: int
    debt_window_days: int
    debt_status: SleepStatus
    debt_is_lower_bound: bool
    formula_version: str = FORMULA_VERSION


def build_sleep_summary(
    *,
    local_health_day: str,
    duration_min: float,
    window: list[DailySleep],
    target_min: int = DEFAULT_TARGET_MIN,
) -> SleepSummary:
    """Assemble the day's sleep summary from its duration and the debt window."""
    debt = sleep_debt_14d(window, target_min=target_min)
    return SleepSummary(
        local_health_day=local_health_day,
        duration_min=duration_min,
        target_min=target_min,
        adherence_pct=sleep_target_adherence(duration_min=duration_min, target_min=target_min),
        debt_14d_min=debt.debt_min,
        debt_known_days=debt.known_days,
        debt_window_days=debt.window_days,
        debt_status=debt.status,
        debt_is_lower_bound=debt.is_lower_bound,
    )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
