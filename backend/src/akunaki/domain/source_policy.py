"""Source policy: deterministic provider precedence for overlapping facts.

Pure: no I/O, no clock. When more than one provider supplies the same signal for
the same local day, exactly **one** provider is authoritative — the design
forbids averaging providers or silently falling back between them. This module
holds the fixed precedence so that choice is one auditable rule, not scattered
per query.

Precedence is **per metric family**, because the best source depends on what is
being measured and when the device is worn — not on which vendor is "better". A
ring worn only between bedtime and waking is the authoritative overnight sleep
source but records nothing during the day; a training watch measures a workout
best but is not worn to bed; an always-on tracker owns naps and daily activity
precisely because it is never taken off. Overnight sleep and naps are therefore
**separate families**: letting the overnight winner also win naps would suppress
a real daytime record as ``lower_precedence`` and lose it.

This is a pure precedence, not the full ``source_selections`` grain machinery
(deferred) — but it is the same principle: one authoritative source per day per
family, never a blend and never a silent fallback.

``SOURCE_POLICY_VERSION`` pins the precedence so a stored derivation can record
which policy chose a day's source.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

SOURCE_POLICY_VERSION = "source_policy_v0.1.0"

# The metric family a sleep-source decision is recorded under.
SLEEP_METRIC_FAMILY = "sleep_session"

# Daytime sleep is a **separate** family from overnight sleep, because the
# authoritative source differs: a ring worn only overnight cannot record a nap it
# slept through, so letting the overnight winner also win naps would suppress the
# daytime provider's real data as `lower_precedence` and lose the nap entirely.
# The sleep normalizer already marks `is_nap`, so routing a fact to one family or
# the other is deterministic rather than a judgement call.
NAP_METRIC_FAMILY = "nap"

# Daily activity (steps, calories, active minutes) and training sessions.
ACTIVITY_METRIC_FAMILY = "daily_activity"
WORKOUT_METRIC_FAMILY = "workout"

# Selection reason vocabulary (mirrors the ``source_selections`` CHECK).
REASON_POLICY_MATCH = "policy_match"
REASON_ONLY_SOURCE = "only_source"
REASON_MISSING = "missing_authoritative"

# Why a losing candidate was not selected.
ELIGIBLE = "eligible"
INELIGIBLE = "ineligible"

# Per-family provider precedence, most authoritative first. A provider absent
# from a family's tuple is never authoritative for it, so it cannot be selected
# over a listed one — it stays visible as an `ineligible` candidate instead.
#
# The default encodes a wear pattern rather than a ranking of vendors, which is
# why it is data rather than scattered `if`s: a deployment whose user wears their
# devices differently needs a different precedence, not different code.
#
# A provider is listed for a family only where it is genuinely worn for it.
# Padding a family with every provider "just in case" is not harmless: a listed
# provider wins any day the ones above it did not cover, so naming a device that
# was never measuring that family invents an authoritative answer from it.
_DEFAULT_PRECEDENCE: dict[str, tuple[str, ...]] = {
    # Overnight sleep: the ring goes on before bed and off on waking. The
    # always-on tracker is the fallback for nights the ring was not worn.
    SLEEP_METRIC_FAMILY: ("oura", "google_health"),
    # Daytime sleep: the ring is off, so the always-on tracker owns naps. Oura
    # stays as fallback for a nap taken while the ring happened to be on.
    NAP_METRIC_FAMILY: ("google_health", "oura"),
    # Training: only the sports watch is worn for a session, so it is the sole
    # authoritative source — no fallback invents a workout from a step counter.
    WORKOUT_METRIC_FAMILY: ("polar",),
    # Everything else in the day: steps, calories, active minutes. The always-on
    # tracker leads; the watch covers days it alone recorded.
    ACTIVITY_METRIC_FAMILY: ("google_health", "polar", "oura"),
}

# Back-compat alias: the sleep precedence several call sites read directly.
_SLEEP_PRECEDENCE: tuple[str, ...] = _DEFAULT_PRECEDENCE[SLEEP_METRIC_FAMILY]


def precedence_for(metric_family: str) -> tuple[str, ...]:
    """Return the provider precedence for ``metric_family``.

    An unknown family yields an empty precedence rather than raising: every
    provider then ranks as `ineligible`, so an unpoliced family records its
    candidates and selects nothing instead of silently inventing a winner.
    """
    return _DEFAULT_PRECEDENCE.get(metric_family, ())


@dataclass(frozen=True, slots=True)
class MetricFamilyPolicy:
    """The effective precedence for one metric family.

    ``providers`` is ordered most-authoritative first. A provider absent from it
    is never authoritative for this family, so it cannot win a day the listed
    ones cover.
    """

    metric_family: str
    providers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EffectivePolicy:
    """The source policy actually in force, as a user can inspect it."""

    policy_version: str
    families: tuple[MetricFamilyPolicy, ...]


def effective_policy() -> EffectivePolicy:
    """Describe the source precedence this build actually enforces.

    Reports every family with a **real** precedence rule — the rule this build
    would actually apply if two providers competed. A family is listed once its
    precedence is consulted at a decision site, not merely because the ADR names
    an authoritative provider for it: listing an unenforced rule would present an
    aspiration as a guarantee, which is exactly what an "inspectable policy" must
    not do.
    """
    return EffectivePolicy(
        policy_version=SOURCE_POLICY_VERSION,
        families=tuple(
            MetricFamilyPolicy(metric_family=family, providers=providers)
            for family, providers in _DEFAULT_PRECEDENCE.items()
        ),
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

    def to_spec(
        self,
        *,
        local_health_day: str,
        metric_family: str = SLEEP_METRIC_FAMILY,
    ) -> DailySelectionSpec:
        """The persistable form of this decision for one local day.

        ``metric_family`` defaults to overnight sleep so existing call sites are
        unchanged; a nap, workout, or activity decision passes its own.
        """
        return DailySelectionSpec(
            metric_family=metric_family,
            local_health_day=local_health_day,
            selected_fact_record_id=self.selected_fact_record_id,
            selection_reason=self.selection_reason,
            missing_reason=self.missing_reason,
            candidates=self.candidates,
        )


def decide_selection(
    facts_by_provider: Mapping[str, list[str]],
    *,
    metric_family: str,
) -> SleepSelection:
    """Choose the day's authoritative fact for ``metric_family``.

    The family-generic form of :func:`decide_sleep_selection`: identical rules,
    but the precedence is looked up per family, so overnight sleep, naps,
    workouts, and daily activity can each have a different authoritative
    provider without duplicating the ranking logic.
    """
    return _decide(facts_by_provider, precedence=precedence_for(metric_family))


def decide_sleep_selection(facts_by_provider: Mapping[str, list[str]]) -> SleepSelection:
    """Choose the day's authoritative **overnight sleep** fact.

    Thin wrapper over :func:`decide_selection` for the sleep family.
    """
    return decide_selection(facts_by_provider, metric_family=SLEEP_METRIC_FAMILY)


def _decide(
    facts_by_provider: Mapping[str, list[str]],
    *,
    precedence: tuple[str, ...],
) -> SleepSelection:
    """Choose the authoritative fact and rank the alternatives.

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
    chosen = highest_precedence(present.keys(), precedence=precedence)

    ranked: list[SelectionCandidate] = []
    rank = 1
    # Eligible providers first, in precedence order.
    for provider in precedence:
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
    for provider in sorted(set(present) - set(precedence)):
        for fact_id in present[provider]:
            ranked.append(
                SelectionCandidate(
                    fact_record_id=fact_id,
                    provider=provider,
                    rank=rank,
                    eligibility=INELIGIBLE,
                    reason="provider_not_in_policy",
                )
            )
            rank += 1

    if chosen is None:
        return SleepSelection(
            selected_fact_record_id=None,
            selection_reason=REASON_MISSING,
            missing_reason=("no_recognized_provider" if present else "no_facts_for_day"),
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
    return highest_precedence(providers_present, precedence=_SLEEP_PRECEDENCE)


def highest_precedence(
    providers_present: Iterable[str],
    *,
    precedence: tuple[str, ...],
) -> str | None:
    """Return the highest-precedence provider present, or None.

    None when no provider in ``precedence`` supplied the family for this day —
    the caller must treat that as unknown, never blending the unrecognized
    sources into an answer the policy did not sanction.
    """
    present = set(providers_present)
    for provider in precedence:
        if provider in present:
            return provider
    return None
