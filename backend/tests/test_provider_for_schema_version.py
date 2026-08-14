"""Which provider a raw schema version belongs to.

Fact rows record the provider that supplied them, and the source policy groups
candidates by that column — so a fact attributed to the wrong provider is
invisible to the precedence meant to rank it. Every fact writer used to hardcode
one provider, which was fine while one connector owned each fact type and wrong
the moment all three began writing daily activity.
"""

from __future__ import annotations

import pytest

from akunaki.domain.connections import Provider, provider_for_schema_version


@pytest.mark.parametrize(
    ("schema_version", "expected"),
    [
        ("oura.v2", "oura"),
        ("oura_activity.v2", "oura"),
        ("polar.v1", "polar"),
        ("polar_activity.v1", "polar"),
        ("google_health.v4", "google_health"),
        ("google_health_activity.v4", "google_health"),
        # Named before the connector's own prefix settled; structural matching
        # alone would attribute it to nothing.
        ("google_activity.v1", "google_health"),
    ],
)
def test_known_schema_versions_map_to_their_provider(schema_version: str, expected: str) -> None:
    assert provider_for_schema_version(schema_version) == expected


def test_nested_prefixes_resolve_to_the_right_provider() -> None:
    """`polar_activity.` also starts with `polar`, and likewise for Google.

    Longest-prefix-first makes this correct by construction rather than by the
    accident of declaration order.
    """
    assert provider_for_schema_version("polar_activity.v1") == Provider.POLAR.value
    assert provider_for_schema_version("google_health_activity.v4") == Provider.GOOGLE_HEALTH.value


@pytest.mark.parametrize("schema_version", ["", "nonsense", "fitbit.v1", "oura2.v1"])
def test_unknown_schema_version_is_none_not_a_default(schema_version: str) -> None:
    """An unattributable fact must not silently acquire some provider.

    Defaulting would put it under a provider that never supplied it, where the
    source policy would rank it against real candidates.
    """
    assert provider_for_schema_version(schema_version) is None
