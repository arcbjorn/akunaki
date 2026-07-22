"""Subjective modifier: the recovery component from a completed check-in (v0.1.0).

Pure: no I/O, no clock. This is the exact v0.1.0 mapping from health-engine.md.

The subjective component draws **only** on an explicit, completed check-in for
the day. It is present only when all three normalized inputs — energy, stress,
and symptom burden — are known. A missing check-in, or a blank symptom field,
omits the component entirely; the design forbids inferring "no symptoms" from
absence, and forbids a neutral 50. Absence is disclosed, never invented.

Each input is on [0, 1] after normalization: energy higher is better, stress
higher is worse, symptom burden higher is worse.
"""

from __future__ import annotations

from dataclasses import dataclass

# Component weights within the subjective blend (exact v0.1.0).
_ENERGY_WEIGHT = 0.5
_STRESS_WEIGHT = 0.25
_SYMPTOM_WEIGHT = 0.25


@dataclass(frozen=True, slots=True)
class SubjectiveInputs:
    """A completed check-in's normalized inputs.

    Each is on [0, 1] or None when unanswered. ``symptom_burden_n = 0`` is only
    valid when the check-in explicitly recorded no symptoms; a blank field must
    be passed as None so the component is omitted rather than assumed benign.
    """

    energy_n: float | None
    stress_n: float | None
    symptom_burden_n: float | None


@dataclass(frozen=True, slots=True)
class SubjectiveComponent:
    """The subjective component score, or its absence."""

    present: bool
    c: float | None


def subjective_component(inputs: SubjectiveInputs) -> SubjectiveComponent:
    """Map a completed check-in to the subjective component, or omit it.

    Omitted (``present=False``) unless energy, stress, and symptom burden are
    all present, each validated to [0, 1]. This is the only path that produces a
    subjective ``c``; there is no default.
    """
    energy = inputs.energy_n
    stress = inputs.stress_n
    symptom = inputs.symptom_burden_n
    if energy is None or stress is None or symptom is None:
        return SubjectiveComponent(present=False, c=None)

    for name, value in (("energy_n", energy), ("stress_n", stress), ("symptom_burden_n", symptom)):
        if not 0.0 <= value <= 1.0:
            msg = f"{name} must be in [0, 1]"
            raise ValueError(msg)

    blended = (
        _ENERGY_WEIGHT * energy
        + _STRESS_WEIGHT * (1.0 - stress)
        + _SYMPTOM_WEIGHT * (1.0 - symptom)
    )
    return SubjectiveComponent(present=True, c=_clamp(100.0 * blended, 0.0, 100.0))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
