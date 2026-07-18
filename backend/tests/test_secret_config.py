"""KEK configuration: strict parsing and fail-fast boot.

A process that needs sealing must never fall back to a default, generated, or
silently-empty key. Every malformed input below has to raise rather than
produce a working-but-unprotected sealer.
"""

from __future__ import annotations

import base64

import pytest

from akunaki.adapters.crypto.config import SecretConfigError, build_sealer, parse_keks
from akunaki.adapters.crypto.envelope import KEY_BYTES
from akunaki.config import Settings

KEY_A = base64.b64encode(b"\x01" * KEY_BYTES).decode()
KEY_B = base64.b64encode(b"\x02" * KEY_BYTES).decode()


def _settings(**overrides: str) -> Settings:
    values: dict[str, str] = {"secret_keks": "", "active_kek_version": ""}
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parses_single_kek() -> None:
    keys = parse_keks(f"v1:{KEY_A}")
    assert set(keys) == {"v1"}
    assert keys["v1"] == b"\x01" * KEY_BYTES


def test_parses_multiple_keks_and_tolerates_whitespace() -> None:
    keys = parse_keks(f"  v1:{KEY_A} ,  v2:{KEY_B}  ")
    assert set(keys) == {"v1", "v2"}


def test_empty_config_parses_to_empty_registry() -> None:
    assert parse_keks("") == {}
    assert parse_keks("   ,  ") == {}


def test_rejects_missing_separator() -> None:
    with pytest.raises(SecretConfigError, match="version:base64key"):
        parse_keks(KEY_A)


def test_rejects_blank_version_or_key() -> None:
    with pytest.raises(SecretConfigError, match="version:base64key"):
        parse_keks(f":{KEY_A}")
    with pytest.raises(SecretConfigError, match="version:base64key"):
        parse_keks("v1:")


def test_rejects_invalid_base64() -> None:
    with pytest.raises(SecretConfigError, match="not valid base64"):
        parse_keks("v1:!!!not-base64!!!")


def test_rejects_wrong_key_length() -> None:
    short = base64.b64encode(b"\x01" * 16).decode()
    with pytest.raises(SecretConfigError, match="exactly 32 bytes"):
        parse_keks(f"v1:{short}")


def test_rejects_duplicate_version() -> None:
    with pytest.raises(SecretConfigError, match="duplicate KEK version"):
        parse_keks(f"v1:{KEY_A},v1:{KEY_B}")


def test_parse_errors_never_leak_key_material() -> None:
    short = base64.b64encode(b"\x01" * 16).decode()
    with pytest.raises(SecretConfigError) as exc_info:
        parse_keks(f"v1:{short}")

    rendered = str(exc_info.value)
    assert short not in rendered
    assert "v1" in rendered


# ---------------------------------------------------------------------------
# Sealer construction (fail fast)
# ---------------------------------------------------------------------------


def test_build_sealer_with_single_key_infers_active_version() -> None:
    sealer = build_sealer(_settings(secret_keks=f"v1:{KEY_A}"))
    assert sealer.active_key_version == "v1"
    assert sealer.open(sealer.seal(b"token")) == b"token"


def test_build_sealer_requires_explicit_active_when_multiple() -> None:
    # Ambiguity during rotation must be an error, not a silent guess.
    with pytest.raises(SecretConfigError, match="ACTIVE_KEK_VERSION is required"):
        build_sealer(_settings(secret_keks=f"v1:{KEY_A},v2:{KEY_B}"))


def test_build_sealer_honors_explicit_active_version() -> None:
    sealer = build_sealer(_settings(secret_keks=f"v1:{KEY_A},v2:{KEY_B}", active_kek_version="v2"))
    assert sealer.active_key_version == "v2"
    # Both versions remain readable after rotation.
    assert sealer.open(sealer.seal(b"x")) == b"x"


def test_build_sealer_rejects_unknown_active_version() -> None:
    with pytest.raises(SecretConfigError, match="not present"):
        build_sealer(_settings(secret_keks=f"v1:{KEY_A}", active_kek_version="v9"))


def test_build_sealer_fails_fast_without_configuration() -> None:
    # No key configured must never yield a usable sealer.
    with pytest.raises(SecretConfigError, match="no envelope-encryption KEK configured"):
        build_sealer(_settings())


def test_settings_default_has_no_keys() -> None:
    # Keys are never baked into defaults or source.
    settings = Settings()
    assert settings.secret_keks == ""
    assert settings.active_kek_version == ""
