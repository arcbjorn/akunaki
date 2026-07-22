"""Canonical training load from HR-zone durations (``general_recovery_v0.1.0``).

Pure: no I/O, no clock. Canonical load is **always** computed internally from
HR-zone minutes under versioned zone weights — vendor-provided load/training
fields are comparison-only and never the engine's authority. This is the exact
v0.1.0 formula from health-engine.md.

``session_load = Σ_z minutes_z · weight_z`` with the default (unvalidated)
weights Z1=1 … Z5=5. A day's strain-load is the sum of its included session
loads. Confirmed complete rest with coverage is a **known 0**; unknown or
incomplete coverage is **missing** (never treated as zero) — the difference the
ACWR strict-coverage gate depends on.
"""

from __future__ import annotations

from dataclasses import dataclass

FORMULA_VERSION = "general_recovery_v0.1.0"

# Default zone weights (unvalidated), Z1..Z5. Individualized boundaries and any
# revised weights would live in a new formula/config version.
DEFAULT_ZONE_WEIGHTS: tuple[float, float, float, float, float] = (1.0, 2.0, 3.0, 4.0, 5.0)


@dataclass(frozen=True, slots=True)
class ZoneMinutes:
    """Minutes spent in each of the five HR zones for one session.

    All five are required and non-negative; a session with unknown zone
    coverage must not be passed here (it contributes to *missing* coverage, not
    a zero-load session).
    """

    z1: float
    z2: float
    z3: float
    z4: float
    z5: float

    def as_tuple(self) -> tuple[float, float, float, float, float]:
        """The five zone minutes in order."""
        return (self.z1, self.z2, self.z3, self.z4, self.z5)


def session_load(
    zones: ZoneMinutes,
    *,
    weights: tuple[float, float, float, float, float] = DEFAULT_ZONE_WEIGHTS,
) -> float:
    """Canonical session load: the weighted sum of HR-zone minutes."""
    minutes = zones.as_tuple()
    for value in minutes:
        if value < 0:
            msg = "zone minutes must be non-negative"
            raise ValueError(msg)
    return sum(m * w for m, w in zip(minutes, weights, strict=True))


def daily_strain_load(session_loads: list[float]) -> float:
    """A day's strain-load: the sum of its included session loads.

    An empty list is a **confirmed rest with coverage**, which is a known 0.
    Callers must not pass this for a day with unknown coverage — that day is
    *missing*, and the caller omits it entirely rather than summing to zero.
    """
    for value in session_loads:
        if value < 0:
            msg = "session loads must be non-negative"
            raise ValueError(msg)
    return sum(session_loads)
