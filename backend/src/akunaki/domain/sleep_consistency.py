"""Sleep consistency: circular regularity of principal-sleep midpoints.

Pure: no I/O, no clock. Consistency measures how tightly a person's sleep
midpoints cluster on the 24-hour clock — a regular schedule scores high, an
erratic one low. These are the exact v0.1.0 formulas from health-engine.md.

The metric is the **mean resultant length** ``R`` of the midpoint angles: each
night's local-time midpoint is placed on the circle ``[0, 1440)`` minutes,
converted to an angle, and the vector mean's length is taken. ``R`` ranges
[0, 1]; the component score is ``100 * R``.

Unlike the vitals components, consistency uses **no baseline** — it is already a
direct 0-100 score. It requires at least 7 valid nights in the 14-day window;
with fewer, the component is omitted (never a midpoint), matching the design's
"insufficient for component use" rule.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# The consistency window matches the sleep-debt window: 14 calendar days.
WINDOW_DAYS = 14

# Minimum valid nights (principal midpoint known) for the component to be used.
MIN_VALID_NIGHTS = 7

# One clock day in minutes; sleep midpoints live on ``[0, MINUTES_PER_DAY)``.
MINUTES_PER_DAY = 1440


@dataclass(frozen=True, slots=True)
class SleepConsistencyResult:
    """A windowed sleep-consistency computation."""

    valid_nights: int
    resultant_length: float | None
    """The mean resultant length R in [0, 1]; None when nights < the minimum."""
    score: float | None
    """The 0-100 component score (100*R); None when nights < the minimum."""

    @property
    def is_usable(self) -> bool:
        """Whether the window has enough valid nights to contribute a component."""
        return self.score is not None


def midpoint_local_minutes(
    *,
    start_local_minutes: float,
    duration_minutes: float,
) -> float:
    """Local-time midpoint of a session on the circle ``[0, 1440)`` minutes.

    ``start_local_minutes`` is minutes past local midnight at sleep onset;
    ``duration_minutes`` is the session span. The midpoint wraps around the
    clock, so a night starting at 23:00 and ending at 07:00 yields a 03:00
    midpoint, not a negative or out-of-range value.
    """
    if duration_minutes < 0:
        msg = "duration_minutes must be non-negative"
        raise ValueError(msg)
    raw = start_local_minutes + duration_minutes / 2.0
    return raw % MINUTES_PER_DAY


def sleep_consistency(midpoints_local_minutes: list[float]) -> SleepConsistencyResult:
    """Mean resultant length and score over a window's principal-sleep midpoints.

    Each element is one valid night's local-time midpoint in ``[0, 1440)``.
    Callers supply only valid nights (principal midpoint known); the count is
    the number of valid nights. Fewer than ``MIN_VALID_NIGHTS`` yields an
    unusable result with null score.
    """
    valid_nights = len(midpoints_local_minutes)
    if valid_nights < MIN_VALID_NIGHTS:
        return SleepConsistencyResult(
            valid_nights=valid_nights,
            resultant_length=None,
            score=None,
        )

    angles = [2.0 * math.pi * m / MINUTES_PER_DAY for m in midpoints_local_minutes]
    mean_cos = sum(math.cos(a) for a in angles) / valid_nights
    mean_sin = sum(math.sin(a) for a in angles) / valid_nights
    resultant = math.sqrt(mean_cos * mean_cos + mean_sin * mean_sin)
    # R is mathematically in [0, 1]; clamp defends against float drift only.
    resultant = min(1.0, max(0.0, resultant))
    return SleepConsistencyResult(
        valid_nights=valid_nights,
        resultant_length=resultant,
        score=100.0 * resultant,
    )
