"""Tests for the deterministic sleep-provider precedence."""

from __future__ import annotations

from akunaki.domain.source_policy import (
    SOURCE_POLICY_VERSION,
    authoritative_sleep_provider,
    decide_sleep_selection,
)


def test_policy_version_is_pinned() -> None:
    assert SOURCE_POLICY_VERSION == "source_policy_v0.1.0"


def test_oura_wins_over_google_health() -> None:
    # Oura is the overnight-authoritative sleep source; it wins any day it
    # covers, so two providers never blend.
    assert authoritative_sleep_provider({"oura", "google_health"}) == "oura"
    assert authoritative_sleep_provider(["google_health", "oura"]) == "oura"


def test_google_health_is_the_fallback() -> None:
    assert authoritative_sleep_provider({"google_health"}) == "google_health"


def test_single_provider_is_chosen() -> None:
    assert authoritative_sleep_provider({"oura"}) == "oura"


def test_no_recognized_provider_is_none() -> None:
    # An unlisted provider is never authoritative for sleep.
    assert authoritative_sleep_provider({"polar"}) is None
    assert authoritative_sleep_provider(set()) is None


def test_unrecognized_provider_never_beats_a_listed_one() -> None:
    assert authoritative_sleep_provider({"polar", "google_health"}) == "google_health"


# ---------------------------------------------------------------------------
# The recorded decision: winner plus inspectable alternatives
# ---------------------------------------------------------------------------


def test_conflict_selects_oura_and_keeps_the_loser() -> None:
    """Two providers cover the night: one wins, the other stays inspectable."""
    decision = decide_sleep_selection({"oura": ["o1"], "google_health": ["g1"]})

    assert decision.selected_fact_record_id == "o1"
    assert decision.selection_reason == "policy_match"
    assert decision.missing_reason is None
    # The losing provider is kept as a candidate — never averaged, never a
    # silent fallback, but visible in the "Why".
    assert [(c.fact_record_id, c.rank, c.reason) for c in decision.candidates] == [
        ("o1", 1, "selected"),
        ("g1", 2, "lower_precedence"),
    ]
    assert all(c.eligibility == "eligible" for c in decision.candidates)


def test_single_provider_is_only_source() -> None:
    """Nothing competed, so the reason distinguishes it from a resolved conflict."""
    decision = decide_sleep_selection({"google_health": ["g1"]})

    assert decision.selected_fact_record_id == "g1"
    assert decision.selection_reason == "only_source"
    assert [c.rank for c in decision.candidates] == [1]


def test_no_facts_is_missing_authoritative() -> None:
    decision = decide_sleep_selection({})

    assert decision.selected_fact_record_id is None
    assert decision.selection_reason == "missing_authoritative"
    assert decision.missing_reason == "no_sleep_facts_for_day"
    assert decision.candidates == ()


def test_unrecognized_provider_is_missing_not_a_fallback() -> None:
    """A provider outside the sleep policy must never be selected.

    It is still recorded as an ineligible candidate, so the day reads as "no
    recognized source" rather than "no data" — a real distinction for the
    data-quality surface.
    """
    decision = decide_sleep_selection({"polar": ["p1"]})

    assert decision.selected_fact_record_id is None
    assert decision.selection_reason == "missing_authoritative"
    assert decision.missing_reason == "no_recognized_sleep_provider"
    assert [(c.fact_record_id, c.eligibility) for c in decision.candidates] == [
        ("p1", "ineligible")
    ]


def test_ineligible_providers_rank_after_eligible_ones() -> None:
    decision = decide_sleep_selection({"polar": ["p1"], "google_health": ["g1"]})

    assert decision.selected_fact_record_id == "g1"
    # google_health is eligible and ranks first despite the dict ordering.
    assert [(c.provider, c.rank, c.eligibility) for c in decision.candidates] == [
        ("google_health", 1, "eligible"),
        ("polar", 2, "ineligible"),
    ]


def test_empty_fact_lists_are_not_present_providers() -> None:
    """A provider with no facts for the day did not cover it."""
    decision = decide_sleep_selection({"oura": [], "google_health": ["g1"]})

    assert decision.selected_fact_record_id == "g1"
    assert decision.selection_reason == "only_source"


def test_decision_is_deterministic() -> None:
    """Same input, same rows — so a re-record dedupes instead of versioning."""
    facts = {"google_health": ["g1", "g2"], "oura": ["o1"]}
    first = decide_sleep_selection(facts)
    second = decide_sleep_selection(dict(reversed(list(facts.items()))))

    assert first == second
