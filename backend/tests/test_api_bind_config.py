"""API bind settings: loopback by default, env-overridable for containers."""

from __future__ import annotations

import pytest

from akunaki.config import Settings


def test_bind_defaults_are_loopback() -> None:
    settings = Settings(database_url="sqlite+libsql:///:memory:")
    assert settings.api_host == "127.0.0.1"
    assert settings.api_port == 8000


def test_bind_is_env_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    # The all-interfaces bind a container sets explicitly; the S104 flag is
    # the point of the test, not an accident.
    all_interfaces = "0.0.0.0"  # noqa: S104
    monkeypatch.setenv("AKUNAKI_API_HOST", all_interfaces)
    monkeypatch.setenv("AKUNAKI_API_PORT", "8080")
    settings = Settings(database_url="sqlite+libsql:///:memory:")
    assert settings.api_host == all_interfaces
    assert settings.api_port == 8080


def test_bind_port_is_range_checked() -> None:
    with pytest.raises(ValueError, match="api_port"):
        Settings(database_url="sqlite+libsql:///:memory:", api_port=0)
