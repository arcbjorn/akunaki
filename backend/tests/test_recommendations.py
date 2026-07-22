"""Golden tests for deterministic recommendation selection (v0.1.0).

Cover each rule's exact predicate, the one-primary invariant, conflict-group
suppression, and the "missing data never rests" rule.
"""

from __future__ import annotations

from akunaki.domain.recommendations import (
    ConflictGroup,
    RecommendationInputs,
    Role,
    RuleId,
    select_recommendations,
)


def _inputs(**overrides: object) -> RecommendationInputs:
    base: dict[str, object] = {
        "sleep_debt_min": None,
        "debt_known_days": 0,
        "sleep_adherence_pct": None,
        "acwr": None,
        "hrv_component_c": None,
        "training_label_is_rest": False,
        "has_data_gap": False,
    }
    base.update(overrides)
    return RecommendationInputs(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Individual predicates
# ---------------------------------------------------------------------------


def test_no_rule_fires_yields_no_primary() -> None:
    result = select_recommendations(_inputs())
    assert result.primary is None
    assert result.supporting == ()


def test_sleep_extend_window_fires() -> None:
    result = select_recommendations(
        _inputs(
            sleep_debt_min=150.0,
            debt_known_days=14,
            sleep_adherence_pct=85.0,
        )
    )
    assert result.primary is not None
    assert result.primary.rule_id is RuleId.SLEEP_EXTEND_WINDOW


def test_sleep_extend_window_needs_12_known_days() -> None:
    result = select_recommendations(
        _inputs(sleep_debt_min=150.0, debt_known_days=11, sleep_adherence_pct=85.0)
    )
    assert result.primary is None


def test_sleep_extend_window_needs_low_adherence() -> None:
    # Adherence >= 90 does not fire even with high debt.
    result = select_recommendations(
        _inputs(sleep_debt_min=150.0, debt_known_days=14, sleep_adherence_pct=95.0)
    )
    assert result.primary is None


def test_load_ease_fires() -> None:
    result = select_recommendations(_inputs(acwr=1.5, hrv_component_c=30.0))
    assert result.primary is not None
    assert result.primary.rule_id is RuleId.LOAD_EASE


def test_load_ease_needs_weak_hrv() -> None:
    # ACWR red but a strong HRV component does not fire load_ease.
    assert select_recommendations(_inputs(acwr=1.5, hrv_component_c=80.0)).primary is None


def test_rest_day_fires_from_label() -> None:
    result = select_recommendations(_inputs(training_label_is_rest=True))
    assert result.primary is not None
    assert result.primary.rule_id is RuleId.REST_DAY


def test_data_gap_reconnect_fires() -> None:
    result = select_recommendations(_inputs(has_data_gap=True))
    assert result.primary is not None
    assert result.primary.rule_id is RuleId.DATA_GAP_RECONNECT


# ---------------------------------------------------------------------------
# Conflict resolution
# ---------------------------------------------------------------------------


def test_rest_outranks_load_ease_in_the_load_group() -> None:
    # Both are in the load group: rest_day (100) wins, load_ease suppressed.
    result = select_recommendations(
        _inputs(training_label_is_rest=True, acwr=1.5, hrv_component_c=30.0)
    )
    assert result.primary is not None
    assert result.primary.rule_id is RuleId.REST_DAY
    suppressed_ids = {r.rule_id for r in result.suppressed}
    assert RuleId.LOAD_EASE in suppressed_ids
    load_ease = next(r for r in result.suppressed if r.rule_id is RuleId.LOAD_EASE)
    assert load_ease.suppressed_by is RuleId.REST_DAY
    assert load_ease.conflict_group is ConflictGroup.LOAD


def test_load_primary_with_sleep_supporting() -> None:
    # A load-group rest primary plus a sleep rule that survives as supporting.
    result = select_recommendations(
        _inputs(
            training_label_is_rest=True,
            sleep_debt_min=150.0,
            debt_known_days=14,
            sleep_adherence_pct=85.0,
        )
    )
    assert result.primary is not None
    assert result.primary.rule_id is RuleId.REST_DAY
    supporting_ids = {r.rule_id for r in result.supporting}
    assert RuleId.SLEEP_EXTEND_WINDOW in supporting_ids
    assert all(r.role is Role.SUPPORTING for r in result.supporting)


def test_data_gap_is_lowest_priority_primary() -> None:
    # With a health rule present, the data-gap rule is only supporting.
    result = select_recommendations(
        _inputs(
            sleep_debt_min=150.0,
            debt_known_days=14,
            sleep_adherence_pct=85.0,
            has_data_gap=True,
        )
    )
    assert result.primary is not None
    assert result.primary.rule_id is RuleId.SLEEP_EXTEND_WINDOW
    supporting_ids = {r.rule_id for r in result.supporting}
    assert RuleId.DATA_GAP_RECONNECT in supporting_ids


def test_exactly_one_primary() -> None:
    result = select_recommendations(
        _inputs(
            training_label_is_rest=True,
            acwr=1.5,
            hrv_component_c=30.0,
            sleep_debt_min=150.0,
            debt_known_days=14,
            sleep_adherence_pct=85.0,
            has_data_gap=True,
        )
    )
    primaries = [
        r
        for r in (result.primary, *result.supporting, *result.suppressed)
        if r.role is Role.PRIMARY
    ]
    assert len(primaries) == 1
