"""Tests for the pure Google push-token claim validation."""

from __future__ import annotations

from datetime import UTC, datetime

from akunaki.domain.google_push import (
    PushRejection,
    validate_google_push_claims,
)

NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
AUD = "https://api.example.com/webhooks/google_health/conn-1"
SA = "push@project.iam.gserviceaccount.com"


def _claims(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "iss": "https://accounts.google.com",
        "aud": AUD,
        "exp": int(NOW.timestamp()) + 300,
        "email": SA,
        "email_verified": True,
    }
    base.update(overrides)
    return base


def test_valid_claims_pass() -> None:
    result = validate_google_push_claims(
        _claims(), expected_audience=AUD, expected_service_account=SA, now=NOW
    )
    assert result.ok


def test_bare_issuer_spelling_is_accepted() -> None:
    result = validate_google_push_claims(
        _claims(iss="accounts.google.com"),
        expected_audience=AUD,
        expected_service_account=SA,
        now=NOW,
    )
    assert result.ok


def test_wrong_issuer_is_rejected() -> None:
    result = validate_google_push_claims(
        _claims(iss="https://evil.example.com"),
        expected_audience=AUD,
        expected_service_account=SA,
        now=NOW,
    )
    assert result.rejection is PushRejection.ISSUER_MISMATCH


def test_wrong_audience_is_rejected() -> None:
    result = validate_google_push_claims(
        _claims(aud="https://other.example.com"),
        expected_audience=AUD,
        expected_service_account=SA,
        now=NOW,
    )
    assert result.rejection is PushRejection.AUDIENCE_MISMATCH


def test_audience_list_is_accepted() -> None:
    result = validate_google_push_claims(
        _claims(aud=["other", AUD]),
        expected_audience=AUD,
        expected_service_account=SA,
        now=NOW,
    )
    assert result.ok


def test_expired_token_is_rejected() -> None:
    result = validate_google_push_claims(
        _claims(exp=int(NOW.timestamp()) - 300),
        expected_audience=AUD,
        expected_service_account=SA,
        now=NOW,
    )
    assert result.rejection is PushRejection.EXPIRED


def test_missing_exp_is_malformed() -> None:
    claims = _claims()
    del claims["exp"]
    result = validate_google_push_claims(
        claims, expected_audience=AUD, expected_service_account=SA, now=NOW
    )
    assert result.rejection is PushRejection.MALFORMED


def test_wrong_service_account_is_rejected() -> None:
    result = validate_google_push_claims(
        _claims(email="attacker@evil.example.com"),
        expected_audience=AUD,
        expected_service_account=SA,
        now=NOW,
    )
    assert result.rejection is PushRejection.EMAIL_MISMATCH


def test_unverified_email_is_rejected() -> None:
    result = validate_google_push_claims(
        _claims(email_verified=False),
        expected_audience=AUD,
        expected_service_account=SA,
        now=NOW,
    )
    assert result.rejection is PushRejection.EMAIL_UNVERIFIED
