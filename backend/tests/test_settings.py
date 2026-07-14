"""Settings / AKUNAKI_ configuration and local database_url validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from akunaki.config import DEFAULT_DATABASE_URL, Settings, clear_settings_cache, get_settings


def test_default_database_url_is_safe_local_libsql() -> None:
    clear_settings_cache()
    settings = Settings()
    assert settings.database_url == DEFAULT_DATABASE_URL
    assert settings.database_url.startswith("sqlite+libsql:")
    assert "model" not in Settings.model_fields
    assert "model_provider" not in Settings.model_fields
    assert "database_auth_token" not in Settings.model_fields


def test_env_prefix_akunaki(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = tmp_path / "env.db"
    url = f"sqlite+libsql:///{db.resolve()}"
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", url)
    monkeypatch.setenv("AKUNAKI_SERVICE_NAME", "test-api")
    clear_settings_cache()
    settings = Settings()
    assert settings.database_url == url
    assert settings.service_name == "test-api"
    clear_settings_cache()


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+libsql://",  # official sqlalchemy-libsql in-memory form
        "sqlite+libsql:///:memory:",
        "sqlite+libsql:///.local/akunaki.db",
        "sqlite+libsql:///rel/path.db",
        "sqlite+libsql:////tmp/akunaki-abs.db",
        f"sqlite+libsql:///{Path('/var/lib/akunaki/file.db').as_posix()}",
    ],
)
def test_accepts_local_libsql_url_forms(url: str) -> None:
    settings = Settings(database_url=url)
    assert settings.database_url == url


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///./plain.db",
        "postgresql://localhost/db",
        "sqlite+aiosqlite:///:memory:",
        "libsql://my-db.turso.io",
    ],
)
def test_rejects_non_libsql_dialect(url: str) -> None:
    with pytest.raises(ValidationError, match="local sqlite\\+libsql"):
        Settings(database_url=url)


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+libsql://host.example.com",
        "sqlite+libsql://host.example.com/db",
        "sqlite+libsql://my-db-user.turso.io",
        "sqlite+libsql://libsql://my-db.turso.io?secure=true",
        "sqlite+libsql://user:pass@host.example.com/db",
        "sqlite+libsql://user:token@my-db.turso.io",
        "sqlite+libsql://host.example.com:8080/db",
        "sqlite+libsql://user:pass@host:8080/db",
        "sqlite+libsql://localhost/foo.db",
        "sqlite+libsql://127.0.0.1/foo.db",
        "sqlite+libsql://[::1]/foo.db",
    ],
)
def test_rejects_remote_host_credential_or_port_urls(url: str) -> None:
    with pytest.raises(ValidationError, match="local sqlite\\+libsql"):
        Settings(database_url=url)


@pytest.mark.parametrize(
    "url",
    [
        # Local-looking paths that carry remote/sync credentials or params.
        "sqlite+libsql://?authToken=secret",
        "sqlite+libsql:///?authToken=secret",
        "sqlite+libsql:///:memory:?authToken=secret",
        "sqlite+libsql:///.local/akunaki.db?authToken=secret",
        "sqlite+libsql:///.local/akunaki.db?syncUrl=https://example.turso.io",
        "sqlite+libsql:///.local/akunaki.db?secure=true",
        "sqlite+libsql:///.local/akunaki.db?foo=bar",
        "sqlite+libsql:////tmp/akunaki.db?authToken=x&syncUrl=y",
        "sqlite+libsql://#frag",
        "sqlite+libsql:///:memory:#frag",
        "sqlite+libsql:///.local/akunaki.db#fragment",
        "sqlite+libsql:///.local/akunaki.db?authToken=x#frag",
    ],
)
def test_rejects_query_or_fragment_on_local_looking_urls(url: str) -> None:
    """Query strings and fragments must not enable remote/sync credentials."""
    with pytest.raises(ValidationError, match="local sqlite\\+libsql"):
        Settings(database_url=url)


def test_get_settings_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_settings_cache()
    monkeypatch.setenv("AKUNAKI_SERVICE_NAME", "cached")
    a = get_settings()
    b = get_settings()
    assert a is b
    clear_settings_cache()
