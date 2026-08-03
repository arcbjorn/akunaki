"""Confirmation binding rules (pure domain).

A confirmation authorizes one specific call. These cover the four rules the
design states: the user confirms out-of-band, execution reauthorizes against
the same binding (arg substitution fails), a model cannot confirm, and replay
of a consumed confirmation fails.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from akunaki.domain.confirmations import (
    ConfirmationBinding,
    ConfirmationRejection,
    ConfirmationStatus,
    canonical_args_hash,
    check_confirmation,
)

T0 = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
EXPIRES = T0 + timedelta(minutes=5)


def _binding(**overrides: object) -> ConfirmationBinding:
    values: dict[str, object] = {
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "run_id": None,
        "tool_name": "privacy.delete",
        "args_hash": canonical_args_hash({"confirm": True}),
        "idempotency_key": "idem-1",
    }
    values.update(overrides)
    return ConfirmationBinding(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Canonical args hash
# ---------------------------------------------------------------------------


def test_key_order_does_not_change_the_hash() -> None:
    """The user approves values, not a serialization.

    If key order mattered, the same approved arguments could fail to match
    themselves across a JSON round trip.
    """
    assert canonical_args_hash({"a": 1, "b": 2}) == canonical_args_hash({"b": 2, "a": 1})


def test_different_values_hash_differently() -> None:
    assert canonical_args_hash({"day": "2026-07-25"}) != canonical_args_hash({"day": "2026-07-26"})


def test_nested_structures_are_canonicalized() -> None:
    left = {"outer": {"z": 1, "a": [1, 2]}}
    right = {"outer": {"a": [1, 2], "z": 1}}
    assert canonical_args_hash(left) == canonical_args_hash(right)


def test_empty_args_have_a_stable_hash() -> None:
    """A no-argument tool still gets a real binding, not an empty string."""
    assert canonical_args_hash({}) == canonical_args_hash({})
    assert len(canonical_args_hash({})) == 64


def test_non_json_values_are_rejected() -> None:
    """Coercing an unexpected type would authorize something not approved."""
    with pytest.raises(TypeError, match="not JSON serializable"):
        canonical_args_hash({"when": datetime.now(UTC)})


def test_nan_is_rejected() -> None:
    """NaN never equals itself, so a NaN argument could never re-match."""
    with pytest.raises(ValueError, match="not JSON compliant"):
        canonical_args_hash({"amount": float("nan")})


# ---------------------------------------------------------------------------
# Binding checks
# ---------------------------------------------------------------------------


def test_matching_binding_authorizes() -> None:
    assert (
        check_confirmation(
            stored=_binding(),
            status=ConfirmationStatus.PENDING,
            expires_at=EXPIRES,
            requested=_binding(),
            now=T0,
        )
        is None
    )


def test_consumed_confirmation_cannot_be_replayed() -> None:
    """Rule 4: one-time use."""
    assert (
        check_confirmation(
            stored=_binding(),
            status=ConfirmationStatus.CONSUMED,
            expires_at=EXPIRES,
            requested=_binding(),
            now=T0,
        )
        is ConfirmationRejection.ALREADY_CONSUMED
    )


def test_cancelled_confirmation_does_not_authorize() -> None:
    assert (
        check_confirmation(
            stored=_binding(),
            status=ConfirmationStatus.CANCELLED,
            expires_at=EXPIRES,
            requested=_binding(),
            now=T0,
        )
        is ConfirmationRejection.CANCELLED
    )


def test_expiry_is_inclusive_at_the_deadline() -> None:
    """Dead exactly at the deadline, not one tick after."""
    assert (
        check_confirmation(
            stored=_binding(),
            status=ConfirmationStatus.PENDING,
            expires_at=EXPIRES,
            requested=_binding(),
            now=EXPIRES,
        )
        is ConfirmationRejection.EXPIRED
    )
    # A moment earlier still authorizes.
    assert (
        check_confirmation(
            stored=_binding(),
            status=ConfirmationStatus.PENDING,
            expires_at=EXPIRES,
            requested=_binding(),
            now=EXPIRES - timedelta(seconds=1),
        )
        is None
    )


def test_argument_substitution_fails() -> None:
    """Rule 2: the exact arguments approved are the ones that may run."""
    approved = _binding(args_hash=canonical_args_hash({"connection_id": "mine"}))
    substituted = _binding(args_hash=canonical_args_hash({"connection_id": "theirs"}))

    assert (
        check_confirmation(
            stored=approved,
            status=ConfirmationStatus.PENDING,
            expires_at=EXPIRES,
            requested=substituted,
            now=T0,
        )
        is ConfirmationRejection.BINDING_MISMATCH
    )


@pytest.mark.parametrize(
    "field",
    ["tenant_id", "user_id", "run_id", "tool_name", "idempotency_key"],
)
def test_every_binding_field_is_load_bearing(field: str) -> None:
    """Changing any one field makes it a different authorized call.

    Without this, a confirmation for one tool, user, or run could authorize
    another — the whole point of binding six fields rather than one.
    """
    requested = _binding(**{field: "different"})

    assert (
        check_confirmation(
            stored=_binding(),
            status=ConfirmationStatus.PENDING,
            expires_at=EXPIRES,
            requested=requested,
            now=T0,
        )
        is ConfirmationRejection.BINDING_MISMATCH
    )


def test_another_users_confirmation_does_not_authorize() -> None:
    """A tenant-mate cannot approve a destructive call on someone's behalf."""
    assert (
        check_confirmation(
            stored=_binding(user_id="user-1"),
            status=ConfirmationStatus.PENDING,
            expires_at=EXPIRES,
            requested=_binding(user_id="user-2"),
            now=T0,
        )
        is ConfirmationRejection.BINDING_MISMATCH
    )


def test_consumed_beats_expired_when_both_apply() -> None:
    """A replayed *and* expired confirmation reports replay.

    Both reject, so the order only decides the reason; asserting it pins the
    behaviour rather than leaving it incidental.
    """
    assert (
        check_confirmation(
            stored=_binding(),
            status=ConfirmationStatus.CONSUMED,
            expires_at=EXPIRES,
            requested=_binding(),
            now=EXPIRES + timedelta(hours=1),
        )
        is ConfirmationRejection.ALREADY_CONSUMED
    )
