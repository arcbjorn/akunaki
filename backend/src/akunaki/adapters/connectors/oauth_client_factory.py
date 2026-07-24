"""Build the right OAuth client for a connector provider.

The linking service is provider-uniform (see ``OAuthClientPort``), so this is
the one place a provider string is mapped to its concrete OAuth client. A
provider without a client here cannot be linked.
"""

from __future__ import annotations

from akunaki.adapters.connectors.google_health import GoogleHealthOAuthClient
from akunaki.adapters.connectors.oura import OuraOAuthClient
from akunaki.adapters.connectors.polar import PolarOAuthClient
from akunaki.config import ConnectorOAuthConfig
from akunaki.ports.oauth_client import OAuthClientPort

# Providers for which a link OAuth client exists.
_LINK_PROVIDERS = frozenset({"oura", "polar", "google_health"})


def supported_link_providers() -> frozenset[str]:
    """Every provider for which a link OAuth client exists."""
    return _LINK_PROVIDERS


def build_oauth_client(provider: str, config: ConnectorOAuthConfig) -> OAuthClientPort:
    """Construct the OAuth client for ``provider`` from its credentials.

    Raises ``ValueError`` for a provider with no linkable client, so an
    unsupported provider fails loudly rather than silently doing nothing.
    """
    if provider == "oura":
        return OuraOAuthClient(client_id=config.client_id, client_secret=config.client_secret)
    if provider == "polar":
        return PolarOAuthClient(client_id=config.client_id, client_secret=config.client_secret)
    if provider == "google_health":
        return GoogleHealthOAuthClient(
            client_id=config.client_id, client_secret=config.client_secret
        )
    supported = ", ".join(sorted(_LINK_PROVIDERS))
    msg = f"no OAuth client for provider {provider!r} (supported: {supported})"
    raise ValueError(msg)
