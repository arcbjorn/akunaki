"""Pure domain types for provider connections.

No I/O, no SQLAlchemy, no crypto imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Provider(StrEnum):
    """Providers this platform can link."""

    OURA = "oura"
    GOOGLE_HEALTH = "google_health"
    POLAR = "polar"


class ConnectionStatus(StrEnum):
    """Lifecycle status of one provider connection."""

    PENDING = "pending"
    ACTIVE = "active"
    NEEDS_REAUTH = "needs_reauth"
    REVOKED = "revoked"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class LinkedConnection:
    """A connection row after a successful link or relink."""

    connection_id: str
    tenant_id: str
    provider: Provider
    status: ConnectionStatus
    scopes: tuple[str, ...]
    external_user_id: str | None


# Schema-version prefixes whose provider is not recoverable from the name alone.
# `google_activity.` is the Google Health daily-activity schema, named before the
# connector's own prefix settled; matching it structurally would resolve to no
# provider at all, silently making those facts invisible to source selection.
_SCHEMA_PROVIDER_ALIASES: dict[str, str] = {
    "google_activity.": Provider.GOOGLE_HEALTH.value,
}


def provider_for_schema_version(schema_version: str) -> str | None:
    """Return the provider a raw schema version belongs to, or None.

    Fact rows record which provider supplied them, and the source policy groups
    candidates by that column — so a fact written under the wrong provider is
    invisible to the precedence that should rank it. Several fact writers serve
    more than one provider now (every connector writes daily activity), which
    makes a hardcoded provider per writer wrong; the schema version already
    travels with every revision, so it is the honest source of that answer.

    Matching is **longest-prefix-first** because the prefixes nest:
    ``polar_activity.v1`` also starts with ``polar``, and
    ``google_health_activity.v4`` with ``google_health``. Iterating in
    declaration order would resolve both to the shorter provider name — which
    happens to be the right answer here, but only by luck; longest-first makes
    it correct by construction rather than by coincidence.

    Returns None for an unrecognized schema version, so a caller must decide
    what an unattributable fact means instead of silently getting a default.
    """
    if not schema_version:
        return None
    for prefix, provider_name in _SCHEMA_PROVIDER_ALIASES.items():
        if schema_version.startswith(prefix):
            return provider_name
    for provider in sorted(Provider, key=lambda p: len(p.value), reverse=True):
        if schema_version == provider.value or schema_version.startswith(f"{provider.value}."):
            return provider.value
        if schema_version.startswith(f"{provider.value}_"):
            return provider.value
    return None
