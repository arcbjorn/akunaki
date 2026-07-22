"""Golden tests for the deterministic training label (v0.1.0).

Every case is derived from the exact band + ordered-downshift rules in
health-engine.md, including the two invariants: missing data never yields rest,
and a high-severity anomaly alone floors at light.
"""

from __future__ import annotations

from akunaki.domain.training_label import (
    TrainingInputs,
    TrainingLabel,
    training_label,
)


def _inputs(**overrides: object) -> TrainingInputs:
    base: dict[str, object] = {
        "recovery_score": 80,
        "recovery_status": "ok",
        "confidence": 0.8,
        "has_high_severity_anomaly": False,
        "symptom_burden_n": 0.0,
        "severe_symptom_flag": False,
        "acwr": None,
        "hrv_component_c": 80.0,
    }
    base.update(overrides)
    return TrainingInputs(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Base bands
# ---------------------------------------------------------------------------


def test_null_score_is_insufficient() -> None:
    result = training_label(_inputs(recovery_score=None, recovery_status="insufficient"))
    assert result.label is TrainingLabel.INSUFFICIENT


def test_low_confidence_is_insufficient() -> None:
    assert training_label(_inputs(confidence=0.3)).label is TrainingLabel.INSUFFICIENT


def test_score_below_40_is_rest() -> None:
    assert training_label(_inputs(recovery_score=39)).label is TrainingLabel.REST


def test_light_band() -> None:
    assert training_label(_inputs(recovery_score=40)).label is TrainingLabel.LIGHT
    assert training_label(_inputs(recovery_score=54)).label is TrainingLabel.LIGHT


def test_moderate_band() -> None:
    assert training_label(_inputs(recovery_score=55)).label is TrainingLabel.MODERATE
    assert training_label(_inputs(recovery_score=74)).label is TrainingLabel.MODERATE


def test_hard_band_with_full_gate() -> None:
    assert training_label(_inputs(recovery_score=75)).label is TrainingLabel.HARD


# ---------------------------------------------------------------------------
# Hard gate
# ---------------------------------------------------------------------------


def test_hard_capped_to_moderate_on_low_confidence() -> None:
    assert training_label(_inputs(recovery_score=90, confidence=0.6)).label is (
        TrainingLabel.MODERATE
    )


def test_hard_capped_on_non_ok_status() -> None:
    assert training_label(_inputs(recovery_score=90, recovery_status="partial")).label is (
        TrainingLabel.MODERATE
    )


# ---------------------------------------------------------------------------
# Downshift invariants
# ---------------------------------------------------------------------------


def test_high_anomaly_floors_at_light_never_rest() -> None:
    # A strong score with a high-severity anomaly falls to light, not rest.
    result = training_label(_inputs(recovery_score=90, has_high_severity_anomaly=True))
    assert result.label is TrainingLabel.LIGHT


def test_high_symptom_burden_floors_at_light() -> None:
    assert training_label(_inputs(recovery_score=90, symptom_burden_n=0.8)).label is (
        TrainingLabel.LIGHT
    )


def test_severe_symptom_flag_is_rest() -> None:
    # A severe flag forces rest via the base band, even at a high score.
    assert training_label(_inputs(recovery_score=90, severe_symptom_flag=True)).label is (
        TrainingLabel.REST
    )


def test_missing_data_never_rests() -> None:
    # Insufficient stays insufficient even with an anomaly present.
    result = training_label(
        _inputs(
            recovery_score=None,
            recovery_status="insufficient",
            has_high_severity_anomaly=True,
        )
    )
    assert result.label is TrainingLabel.INSUFFICIENT


# ---------------------------------------------------------------------------
# ACWR red band
# ---------------------------------------------------------------------------


def test_acwr_red_caps_hard_to_moderate() -> None:
    # Score would be hard, but ACWR > 1.3 blocks it at the base gate -> moderate.
    result = training_label(_inputs(recovery_score=90, acwr=1.5, hrv_component_c=80.0))
    assert result.label is TrainingLabel.MODERATE


def test_acwr_red_blocks_hard_even_with_weak_hrv() -> None:
    # The ACWR red band blocks hard at the base gate, so a red-band day (even
    # with a weak HRV component) is moderate; step 5's further light drop only
    # fires when the label somehow reached step 5 still as hard, which the base
    # gate prevents. This documents the design's defensive layering.
    result = training_label(
        _inputs(recovery_score=90, acwr=1.5, hrv_component_c=30.0, recovery_status="ok")
    )
    assert result.label is TrainingLabel.MODERATE
