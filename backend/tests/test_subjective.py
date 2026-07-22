"""Golden tests for the subjective modifier (v0.1.0).

Every expected value is computed by hand from health-engine.md. The blend and
the omit-on-missing rule are a stability contract, so these are exact.
"""

from __future__ import annotations

import pytest

from akunaki.domain.subjective import SubjectiveInputs, subjective_component


def test_all_best_scores_100() -> None:
    # energy 1, stress 0, symptom 0 -> 0.5*1 + 0.25*1 + 0.25*1 = 1.0 -> 100.
    result = subjective_component(
        SubjectiveInputs(energy_n=1.0, stress_n=0.0, symptom_burden_n=0.0)
    )
    assert result.present is True
    assert result.c == pytest.approx(100.0)


def test_all_worst_scores_0() -> None:
    # energy 0, stress 1, symptom 1 -> 0.5*0 + 0.25*0 + 0.25*0 = 0 -> 0.
    result = subjective_component(
        SubjectiveInputs(energy_n=0.0, stress_n=1.0, symptom_burden_n=1.0)
    )
    assert result.c == pytest.approx(0.0)


def test_midpoint_blend() -> None:
    # energy 0.6, stress 0.4, symptom 0.2:
    # 0.5*0.6 + 0.25*(1-0.4) + 0.25*(1-0.2) = 0.3 + 0.15 + 0.2 = 0.65 -> 65.
    result = subjective_component(
        SubjectiveInputs(energy_n=0.6, stress_n=0.4, symptom_burden_n=0.2)
    )
    assert result.c == pytest.approx(65.0)


def test_energy_is_weighted_most() -> None:
    # Only energy present at full; stress/symptom neutral 0.5:
    # 0.5*1 + 0.25*0.5 + 0.25*0.5 = 0.75 -> 75.
    result = subjective_component(
        SubjectiveInputs(energy_n=1.0, stress_n=0.5, symptom_burden_n=0.5)
    )
    assert result.c == pytest.approx(75.0)


def test_explicit_zero_symptom_burden_is_valid() -> None:
    # symptom_burden_n = 0 (explicit "no symptoms") is a real input, not missing.
    result = subjective_component(
        SubjectiveInputs(energy_n=0.5, stress_n=0.5, symptom_burden_n=0.0)
    )
    assert result.present is True
    # 0.5*0.5 + 0.25*0.5 + 0.25*1 = 0.25 + 0.125 + 0.25 = 0.625 -> 62.5.
    assert result.c == pytest.approx(62.5)


def test_missing_any_field_omits_the_component() -> None:
    for inputs in (
        SubjectiveInputs(energy_n=None, stress_n=0.5, symptom_burden_n=0.5),
        SubjectiveInputs(energy_n=0.5, stress_n=None, symptom_burden_n=0.5),
        SubjectiveInputs(energy_n=0.5, stress_n=0.5, symptom_burden_n=None),
    ):
        result = subjective_component(inputs)
        assert result.present is False
        assert result.c is None


def test_out_of_range_input_raises() -> None:
    with pytest.raises(ValueError, match="energy_n must be in"):
        subjective_component(SubjectiveInputs(energy_n=1.5, stress_n=0.5, symptom_burden_n=0.5))
    with pytest.raises(ValueError, match="symptom_burden_n must be in"):
        subjective_component(SubjectiveInputs(energy_n=0.5, stress_n=0.5, symptom_burden_n=-0.1))
