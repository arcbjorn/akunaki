"""What each connector actually ingests, as this build enforces it.

Answers the question a user has *before* linking: "if I connect only this
device, what will I get?" Today that is unanswerable — a user can link Polar,
see workouts arrive, and never learn that a recovery score will never appear
because nothing supplies overnight sleep.

**Only code-enforced capabilities.** ``ingestion-and-sync.md`` carries a wide
capability matrix explicitly labelled *proposed targets*: Oura SpO2, Polar swim
structure, Health Connect, HealthKit. None of it is implemented. Publishing it
would present an aspiration as a fact and promise data that never arrives — the
same mistake ``effective_policy`` refuses to make for source precedence.

So this mirrors exactly one thing: the ``_PROVIDER_STREAMS`` dispatch table that
decides what a sync fetches. A capability listed here is one the backfill
actually produces.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "Capability",
    "ProviderCapabilities",
    "capabilities_for",
]


class Capability(StrEnum):
    """A kind of data a connector actually ingests.

    Closed vocabulary: a client renders copy per capability, and these name the
    normalizers that run over a provider's payload — not vendor feature names.
    """

    SLEEP = "sleep"
    """Sleep sessions: durations, stages, efficiency."""

    OVERNIGHT_VITALS = "overnight_vitals"
    """Overnight HRV, resting HR, temperature deviation, respiratory rate."""

    WORKOUTS = "workouts"
    """Workout sessions with canonical zone-load."""


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """What one provider contributes to the engine."""

    provider: str
    capabilities: tuple[Capability, ...]
    supports_recovery_score: bool
    """Whether this provider alone can satisfy the recovery score's gate.

    The v0.1.0 gate needs sleep-target adherence **and** either HRV or resting
    HR — so sleep alone is not enough, and workouts alone are nowhere near it. A
    user linking only such a provider deserves to know that up front rather than
    wondering why their score stays ``insufficient``.

    Necessary, not sufficient: the gate also requires a minimum available
    weight, so even a capable provider yields no score until enough days carry
    real measurements.
    """


# Mirrors ``_PROVIDER_STREAMS`` in ``application/sync_handlers``: the stream a
# provider backfills determines which normalizers run, and therefore what it can
# contribute. Kept as data rather than derived from that table because the
# domain must not import the application layer — the drift risk is covered by a
# test asserting the two stay in step.
_CAPABILITIES: dict[str, tuple[Capability, ...]] = {
    # The Oura sleep stream carries both sleep and the overnight vitals the
    # vitals normalizer extracts from the same payload.
    "oura": (Capability.SLEEP, Capability.OVERNIGHT_VITALS),
    # Polar backfills the AccessLink exercises list only. No sleep, no vitals —
    # this is the connection a user is most likely to misjudge.
    "polar": (Capability.WORKOUTS,),
    # Google Health backfills v4 Fitbit-origin sleep. The connector fetches no
    # vitals stream today, so it cannot satisfy the recovery gate alone.
    "google_health": (Capability.SLEEP,),
}


def capabilities_for(provider: str) -> ProviderCapabilities:
    """Describe one provider, or raise ``KeyError`` for an unknown one.

    Raising is deliberate: a provider with a linkable OAuth client but no entry
    here would otherwise be reported as contributing nothing, which reads as a
    broken connector rather than the wiring gap it is.
    """
    capabilities = _CAPABILITIES[provider]
    return ProviderCapabilities(
        provider=provider,
        capabilities=capabilities,
        # Both halves are required by the recovery gate; sleep alone leaves the
        # score insufficient.
        supports_recovery_score=(
            Capability.SLEEP in capabilities and Capability.OVERNIGHT_VITALS in capabilities
        ),
    )
