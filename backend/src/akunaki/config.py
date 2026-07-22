"""Typed application settings (pydantic-settings, AKUNAKI_ prefix)."""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Safe local default: relative file under the process CWD.
# Parent directory is created when the engine is built, not at settings load.
DEFAULT_DATABASE_URL = "sqlite+libsql:///.local/akunaki.db"


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
