"""Tests for the deterministic sleep-provider precedence."""

from __future__ import annotations

from akunaki.domain.source_policy import (
    SOURCE_POLICY_VERSION,
    authoritative_sleep_provider,
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
