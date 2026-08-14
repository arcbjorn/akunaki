"""When a stored access token is refreshed, and what survives the merge.

Refresh is **proactive**: a token is renewed from its stored expiry rather than
after a 401 has already failed a request. Reacting to the 401 instead would flip
the connection to ``needs_reauth`` — the path demanding manual re-consent — for a
condition a refresh resolves silently.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from akunaki.domain.tokens import (
    OAuthTokens,
    merge_refreshed_tokens,
    needs_refresh,
)

NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)


def test_expired_token_is_due() -> None:
    assert needs_refresh("2026-08-14T11:00:00Z", now=NOW) is True


def test_token_expiring_inside_the_leeway_is_due() -> None:
    """A token that expires mid-sync fails the *next* page, not the first.

    The window therefore has to cover a whole sync run, not just the instant of
    the check.
    """
    assert needs_refresh("2026-08-14T12:02:00Z", now=NOW) is True


def test_token_valid_beyond_the_leeway_is_not_due() -> None:
    assert needs_refresh("2026-08-14T13:00:00Z", now=NOW) is False


def test_token_without_an_expiry_is_never_due() -> None:
    """Providers issuing long-lived tokens (Polar) omit the expiry entirely.

    Refreshing on every sync would burn a grant that never needed one.
    """
    assert needs_refresh(None, now=NOW) is False


def test_unparseable_expiry_is_treated_as_due() -> None:
    """A value we cannot read is not a guarantee that the token is good."""
    assert needs_refresh("not-a-timestamp", now=NOW) is True


def test_leeway_is_configurable() -> None:
    hour_away = "2026-08-14T13:00:00Z"
    assert needs_refresh(hour_away, now=NOW, leeway=timedelta(minutes=5)) is False
    assert needs_refresh(hour_away, now=NOW, leeway=timedelta(hours=2)) is True


# ---------------------------------------------------------------------------
# Merging a refresh response over the stored tokens
# ---------------------------------------------------------------------------


def _stored() -> dict[str, object]:
    return {
        "access_token": "old-access",
        "refresh_token": "the-refresh-token",
        "expires_at": "2026-08-14T11:00:00Z",
        "token_type": "bearer",
    }


def test_refresh_without_a_new_refresh_token_keeps_the_stored_one() -> None:
    """Google returns no ``refresh_token`` on a refresh — that is normal.

    Writing the response's ``None`` straight through would destroy the only
    means of ever refreshing again, turning a working connection into one
    needing manual re-consent at the next expiry.
    """
    fresh = OAuthTokens(
        access_token="new-access",
        refresh_token=None,
        expires_at="2026-08-14T13:00:00Z",
        scopes=(),
        token_type="bearer",
    )

    merged = merge_refreshed_tokens(_stored(), fresh)

    assert merged["access_token"] == "new-access"
    assert merged["refresh_token"] == "the-refresh-token"
    assert merged["expires_at"] == "2026-08-14T13:00:00Z"


def test_a_rotated_refresh_token_replaces_the_stored_one() -> None:
    """A provider that *does* rotate must not keep the spent token."""
    fresh = OAuthTokens(
        access_token="new-access",
        refresh_token="rotated",
        expires_at="2026-08-14T13:00:00Z",
        scopes=(),
        token_type="bearer",
    )

    merged = merge_refreshed_tokens(_stored(), fresh)

    assert merged["refresh_token"] == "rotated"


def test_merge_preserves_unrelated_stored_fields() -> None:
    stored = _stored()
    stored["external_user_id"] = "12345"
    fresh = OAuthTokens(
        access_token="new-access",
        refresh_token=None,
        expires_at=None,
        scopes=(),
        token_type="bearer",
    )

    merged = merge_refreshed_tokens(stored, fresh)

    assert merged["external_user_id"] == "12345"
