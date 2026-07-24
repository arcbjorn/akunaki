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

from collections.abc import Iterable

SOURCE_POLICY_VERSION = "source_policy_v0.1.0"

# Sleep provider precedence, most authoritative first. A provider absent here is
# never authoritative for sleep, so it cannot be selected over a listed one.
_SLEEP_PRECEDENCE: tuple[str, ...] = (
    "oura",
    "google_health",
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
