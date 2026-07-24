"""Typed application settings (pydantic-settings, AKUNAKI_ prefix)."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Safe local default: relative file under the process CWD.
# Parent directory is created when the engine is built, not at settings load.
DEFAULT_DATABASE_URL = "sqlite+libsql:///.local/akunaki.db"


@dataclass(frozen=True, slots=True)
class ConnectorOAuthConfig:
    """A connector's fully-configured OAuth credentials."""

    client_id: str
    client_secret: str
    redirect_uri: str


def _is_local_libsql_url(value: str) -> bool:
    """Return True when value is a local-only sqlite+libsql URL form.

    Accepted forms (no hostname, credentials, port, query, or fragment):
    - official in-memory: ``sqlite+libsql://``
    - path in-memory: ``sqlite+libsql:///:memory:``
    - relative file: ``sqlite+libsql:///rel/path.db``
    - absolute file: ``sqlite+libsql:////abs/path.db``

    Query strings and fragments are always rejected so remote/sync
    credentials (authToken, syncUrl, secure, or arbitrary params) cannot
    be enabled or carried through this foundation.
    """
    if not value.startswith("sqlite+libsql:"):
        return False
    parsed = urlparse(value)
    if parsed.scheme != "sqlite+libsql":
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if parsed.hostname is not None:
        return False
    if parsed.port is not None:
        return False
    # Reject every query string and fragment (authToken, syncUrl, secure, …).
    if parsed.query or parsed.fragment:
        return False
    # Official empty in-memory form from sqlalchemy-libsql: sqlite+libsql://
    # Local file / path-memory forms use empty netloc and a non-empty path.
    # Examples after parse: "/:memory:", "/.local/db", "//abs/path".
    path = parsed.path or ""
    if path == "":
        return True
    # Bare slash-only is not a valid file or memory path.
    return path != "/"


class Settings(BaseSettings):
    """Core process configuration. Model/agent settings are intentionally absent."""

    model_config = SettingsConfigDict(
        env_prefix="AKUNAKI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    service_name: str = Field(default="akunaki-api", description="Service identity for health.")
    database_url: str = Field(
        default=DEFAULT_DATABASE_URL,
        description=(
            "Local SQLAlchemy URL using the official sqlite+libsql dialect only. "
            "Accepted: official in-memory (sqlite+libsql://), "
            "path in-memory (sqlite+libsql:///:memory:), "
            "relative file (sqlite+libsql:///rel/path.db), "
            "or absolute file (sqlite+libsql:////abs/path.db). "
            "Query strings and fragments are rejected. "
            "Remote Turso/host URLs are not accepted in this foundation."
        ),
    )
    echo_sql: bool = Field(default=False, description="Echo SQL to logs (dev only).")
    secret_keks: str = Field(
        default="",
        description=(
            "Envelope-encryption KEKs as 'version:base64key' pairs, comma separated. "
            "Each key must decode to exactly 32 bytes (AES-256). "
            "Empty disables secret sealing; processes that need it fail fast at boot. "
            "Never commit real keys: supply via the platform secret store."
        ),
    )
    active_kek_version: str = Field(
        default="",
        description=(
            "KEK version new envelopes are sealed under. Must be present in secret_keks. "
            "Defaults to the sole configured version when exactly one is supplied."
        ),
    )
    oidc_issuer: str = Field(
        default="",
        description=(
            "OIDC issuer URL (e.g. https://auth.example.com). Empty disables "
            "the login routes: they are not mounted, so there is no half-built "
            "auth surface on an unconfigured deployment."
        ),
    )
    oidc_client_id: str = Field(default="", description="OIDC client id.")
    oidc_client_secret: str = Field(default="", description="OIDC client secret.")
    oidc_redirect_uri: str = Field(
        default="",
        description="Exact callback URI registered with the IdP; must match at the callback.",
    )
    session_cookie_secure: bool = Field(
        default=True,
        description=(
            "Set the Secure attribute on the session cookie. Only turn off for "
            "local HTTP development; a real deployment must keep it true."
        ),
    )
    cors_allowed_origins: tuple[str, ...] = Field(
        default=(),
        description=(
            "Exact browser origins allowed to make credentialed cross-origin "
            "requests (e.g. the PWA origin). Empty means no cross-origin browser "
            "access — a same-origin or server-to-server deployment. Never '*' "
            "with credentials."
        ),
    )
    debug_routes_enabled: bool = Field(
        default=False,
        description=(
            "Mount the unauthenticated internal debug router. "
            "Serves tenant health data with NO authentication, so it must stay "
            "off outside local development. Default off: the routes are not "
            "registered at all unless this is explicitly set."
        ),
    )

    # Per-connector OAuth credentials. Each provider is link-enabled only when
    # its id, secret, and redirect are all set — an unconfigured provider is not
    # mounted, so there is no half-built connect surface (as with OIDC login).
    oura_client_id: str = Field(default="", description="Oura OAuth client id.")
    oura_client_secret: str = Field(default="", description="Oura OAuth client secret.")
    oura_redirect_uri: str = Field(default="", description="Oura OAuth callback URI.")
    polar_client_id: str = Field(default="", description="Polar OAuth client id.")
    polar_client_secret: str = Field(default="", description="Polar OAuth client secret.")
    polar_redirect_uri: str = Field(default="", description="Polar OAuth callback URI.")
    google_health_client_id: str = Field(default="", description="Google Health OAuth client id.")
    google_health_client_secret: str = Field(
        default="", description="Google Health OAuth client secret."
    )
    google_health_redirect_uri: str = Field(
        default="", description="Google Health OAuth callback URI."
    )

    # Per-connector webhook signing secrets (HMAC-SHA256 verification). Empty
    # disables webhook ingress for that provider (the delivery is rejected),
    # so an unconfigured provider has no verifiable webhook path.
    oura_webhook_secret: str = Field(default="", description="Oura webhook signing secret.")
    polar_webhook_secret: str = Field(default="", description="Polar webhook signing secret.")
    # Google Health push (Pub/Sub OIDC) authorization. Both are required to
    # enable the Google Health webhook path; empty disables it (404).
    google_health_push_audience: str = Field(
        default="", description="Expected `aud` on the Google push OIDC token."
    )
    google_health_push_service_account: str = Field(
        default="", description="Expected push service-account email on the token."
    )

    def connector_oauth(self, provider: str) -> ConnectorOAuthConfig | None:
        """Return a provider's OAuth config, or None when not fully configured.

        A provider is link-enabled only when its id, secret, and redirect are
        all present; otherwise it is treated as absent (never a partial surface).
        """
        prefix = provider  # matches the flat field names below
        client_id = getattr(self, f"{prefix}_client_id", "")
        client_secret = getattr(self, f"{prefix}_client_secret", "")
        redirect_uri = getattr(self, f"{prefix}_redirect_uri", "")
        if not (client_id.strip() and client_secret.strip() and redirect_uri.strip()):
            return None
        return ConnectorOAuthConfig(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
        )

    def webhook_secret(self, provider: str) -> str | None:
        """Return a provider's webhook signing secret, or None when unset.

        None means webhook ingress is not enabled for the provider, so a
        delivery cannot be verified and must be rejected.
        """
        secret = getattr(self, f"{provider}_webhook_secret", "")
        return secret if secret.strip() else None

    @field_validator("database_url")
    @classmethod
    def _require_local_libsql_url(cls, value: str) -> str:
        if not _is_local_libsql_url(value):
            msg = (
                "database_url must be a local sqlite+libsql URL "
                "(official in-memory sqlite+libsql://, path in-memory, "
                "relative file, or absolute file). "
                "Hostnames, credentials, ports, query strings, fragments, "
                "and non-sqlite+libsql dialects are rejected."
            )
            raise ValueError(msg)
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()


def clear_settings_cache() -> None:
    """Drop cached settings (tests / process reconfiguration)."""
    get_settings.cache_clear()
