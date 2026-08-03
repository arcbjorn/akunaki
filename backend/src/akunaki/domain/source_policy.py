"""Source policy: deterministic provider precedence for overlapping facts.

Pure: no I/O, no clock. When more than one provider supplies the same signal for
the same local day, exactly **one** provider is authoritative — the design
forbids averaging providers or silently falling back between them. This module
holds the fixed precedence so that choice is one auditable rule, not scattered
per query.

v0.1.0 scope is **sleep**: Oura is the overnight-authoritative sleep source, so
it wins any day it covers; Google Health (Fitbit-origin cloud sleep) is the
fallback for days Oura did not record. This is a pure precedence, not the full
``source_selections`` grain machinery (deferred) — but it is the same principle:
one authoritative source per day, never a blend.

``SOURCE_POLICY_VERSION`` pins the precedence so a stored derivation can record
which policy chose a day's source.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

SOURCE_POLICY_VERSION = "source_policy_v0.1.0"

# The metric family a sleep-source decision is recorded under.
SLEEP_METRIC_FAMILY = "sleep_session"

# Selection reason vocabulary (mirrors the ``source_selections`` CHECK).
REASON_POLICY_MATCH = "policy_match"
REASON_ONLY_SOURCE = "only_source"
REASON_MISSING = "missing_authoritative"

# Why a losing candidate was not selected.
ELIGIBLE = "eligible"
INELIGIBLE = "ineligible"

# Sleep provider precedence, most authoritative first. A provider absent here is
# never authoritative for sleep, so it cannot be selected over a listed one.
_SLEEP_PRECEDENCE: tuple[str, ...] = (
    "oura",
    "google_health",
)


@dataclass(frozen=True, slots=True)
class SelectionCandidate:
    """One competing provider fact, with why it did or did not win.

    ``provider`` is carried for readability at the decision site and in tests;
    it is **not** persisted, because ``source_selection_candidates`` stores only
    the fact FK — the provider is already reachable through it, and duplicating
    it would create a second copy that could drift.
    """

    fact_record_id: str
    rank: int
    eligibility: str
    reason: str
    provider: str = ""


@dataclass(frozen=True, slots=True)
class DailySelectionSpec:
    """A per-day source-selection decision, ready to persist.

    Pure data: the shape the persistence port accepts, defined here so the
    application can build a decision without importing an adapter.
    """

    metric_family: str
    local_health_day: str
    selected_fact_record_id: str | None
    selection_reason: str
    missing_reason: str | None
    candidates: tuple[SelectionCandidate, ...]


@dataclass(frozen=True, slots=True)
class SleepSelection:
    """The day's sleep-source decision, with its alternatives.

    ``selected_fact_record_id`` is None only when no recognized sleep provider
    covered the day — the ADR's ``missing_authoritative``, which carries a
    required ``missing_reason`` and never silently falls back.
    """

    selected_fact_record_id: str | None
    selection_reason: str
    missing_reason: str | None
    candidates: tuple[SelectionCandidate, ...]

    def to_spec(self, *, local_health_day: str) -> DailySelectionSpec:
        """The persistable form of this decision for one local day."""
        return DailySelectionSpec(
            metric_family=SLEEP_METRIC_FAMILY,
            local_health_day=local_health_day,
            selected_fact_record_id=self.selected_fact_record_id,
            selection_reason=self.selection_reason,
            missing_reason=self.missing_reason,
            candidates=self.candidates,
        )


def decide_sleep_selection(facts_by_provider: Mapping[str, list[str]]) -> SleepSelection:
    """Choose the day's authoritative sleep fact and rank the alternatives.

    Pure and total: every provider present becomes a candidate, ranked by the
    fixed precedence, so the losers stay inspectable rather than discarded. A
    provider outside the precedence is recorded ``ineligible`` — it is visible
    as an alternative but can never be selected, which is what keeps "no
    recognized source" distinct from "no data at all".

    The winner is the **first** fact of the highest-precedence provider (ids are
    caller-ordered). Ranking is deterministic: precedence order first, then the
    ineligible providers by name, so the same day always yields the same rows
    and a re-record dedupes instead of writing a new version.
    """
    present = {provider: facts for provider, facts in facts_by_provider.items() if facts}
    chosen = authoritative_sleep_provider(present.keys())

    ranked: list[SelectionCandidate] = []
    rank = 1
    # Eligible providers first, in precedence order.
    for provider in _SLEEP_PRECEDENCE:
        for fact_id in present.get(provider, []):
            ranked.append(
                SelectionCandidate(
                    fact_record_id=fact_id,
                    provider=provider,
                    rank=rank,
                    eligibility=ELIGIBLE,
                    reason=("selected" if provider == chosen else "lower_precedence"),
                )
            )
            rank += 1
    # Then anything the policy does not recognize as a sleep source.
    for provider in sorted(set(present) - set(_SLEEP_PRECEDENCE)):
        for fact_id in present[provider]:
            ranked.append(
                SelectionCandidate(
                    fact_record_id=fact_id,
                    provider=provider,
                    rank=rank,
                    eligibility=INELIGIBLE,
                    reason="provider_not_in_sleep_policy",
                )
            )
            rank += 1

    if chosen is None:
        return SleepSelection(
            selected_fact_record_id=None,
            selection_reason=REASON_MISSING,
            missing_reason=(
                "no_recognized_sleep_provider" if present else "no_sleep_facts_for_day"
            ),
            candidates=tuple(ranked),
        )

    # ``only_source`` when nothing competed for the day; otherwise the
    # precedence actually resolved a conflict.
    reason = REASON_ONLY_SOURCE if len(present) == 1 else REASON_POLICY_MATCH
    return SleepSelection(
        selected_fact_record_id=present[chosen][0],
        selection_reason=reason,
        missing_reason=None,
        candidates=tuple(ranked),
    )


def authoritative_sleep_provider(providers_present: Iterable[str]) -> str | None:
    """Return the one authoritative sleep provider among those present, or None.

    Given the set of providers that supplied sleep for a local day, pick the
    highest-precedence one. None when no recognized sleep provider is present
    (the day has no authoritative sleep — the caller must treat it as unknown,
    never blend the unrecognized sources).
    """
    present = set(providers_present)
    for provider in _SLEEP_PRECEDENCE:
        if provider in present:
            return provider
    return None
