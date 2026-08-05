"""Provider capabilities: what each connector actually ingests.

The value of this surface depends entirely on it describing **implemented**
behaviour. A capability that is aspirational promises data that never arrives,
so the load-bearing test here is the one tying the table to the sync dispatch.
"""

from __future__ import annotations

import pytest

from akunaki.adapters.connectors.oauth_client_factory import supported_link_providers
from akunaki.application.sync_handlers import sync_config_for_provider
from akunaki.domain.provider_capabilities import (
    Capability,
    capabilities_for,
)


def test_oura_supplies_sleep_and_overnight_vitals() -> None:
    """One stream, two normalizers: vitals ride the same sleep payload."""
    described = capabilities_for("oura")

    assert set(described.capabilities) == {Capability.SLEEP, Capability.OVERNIGHT_VITALS}


def test_polar_supplies_workouts_only() -> None:
    """The connection a user is most likely to misjudge."""
    assert capabilities_for("polar").capabilities == (Capability.WORKOUTS,)


def test_google_health_supplies_sleep_without_vitals() -> None:
    """The v4 backfill fetches sleep; no vitals stream is wired."""
    assert capabilities_for("google_health").capabilities == (Capability.SLEEP,)


def test_only_oura_can_reach_the_recovery_gate_alone() -> None:
    """The gate needs sleep adherence plus HRV or resting HR.

    Stated per provider so a user linking Polar or Google Health alone learns
    up front why no score appears, instead of reading ``insufficient`` forever.
    """
    assert capabilities_for("oura").supports_recovery_score is True
    assert capabilities_for("polar").supports_recovery_score is False
    assert capabilities_for("google_health").supports_recovery_score is False


def test_sleep_alone_does_not_claim_a_recovery_score() -> None:
    """Sleep without overnight vitals fails the gate's second half."""
    described = capabilities_for("google_health")

    assert Capability.SLEEP in described.capabilities
    assert described.supports_recovery_score is False


def test_an_unknown_provider_raises() -> None:
    """Silent empty capabilities would read as a broken connector."""
    with pytest.raises(KeyError):
        capabilities_for("fitbit")


def test_every_linkable_provider_is_described() -> None:
    """The guard against drift: a linkable provider must be describable.

    A connector gains an OAuth client and this surface must gain its
    capabilities in the same change — otherwise ``/v1/providers`` raises on a
    provider the user can already link.
    """
    for provider in supported_link_providers():
        described = capabilities_for(provider)
        assert described.provider == provider
        assert described.capabilities, f"{provider} is linkable but claims no capabilities"


def test_claimed_capabilities_match_the_backfilled_stream() -> None:
    """What a provider claims must match what its sync actually fetches.

    Read through the public sync config rather than the private dispatch table,
    so this pins the behaviour a caller sees: the stream that gets backfilled.
    """
    for provider in supported_link_providers():
        stream = sync_config_for_provider(provider).stream
        capabilities = capabilities_for(provider).capabilities
        if stream == "sleep":
            assert Capability.SLEEP in capabilities
            # A sleep backfill never yields workouts.
            assert Capability.WORKOUTS not in capabilities
        if stream == "workout":
            assert Capability.WORKOUTS in capabilities
            # And a workout backfill never yields sleep or overnight vitals.
            assert Capability.SLEEP not in capabilities
            assert Capability.OVERNIGHT_VITALS not in capabilities
