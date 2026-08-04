"""Audit event rules (pure domain).

Two properties matter: the trail must not become a second copy of the health
data, and editing it must be detectable.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from akunaki.domain.audit import (
    GENESIS_HASH,
    ActorType,
    AuditAction,
    AuditEvent,
    InvalidAuditMetadataError,
    chain_hash,
    validate_metadata,
    verify_chain,
)

NOW = "2026-08-04T12:00:00Z"


def _event(
    *,
    event_id: str,
    previous_hash: str,
    action: AuditAction = AuditAction.DELETE,
    resource_id: str | None = "res-1",
    metadata: dict[str, str] | None = None,
) -> AuditEvent:
    meta = metadata if metadata is not None else {"outcome": "completed"}
    digest = chain_hash(
        previous_hash=previous_hash,
        tenant_id="tenant-1",
        actor_type=ActorType.USER,
        actor_id="user-1",
        action=action,
        resource_type="tenant",
        resource_id=resource_id,
        metadata=meta,
        created_at=NOW,
    )
    return AuditEvent(
        event_id=event_id,
        tenant_id="tenant-1",
        actor_type=ActorType.USER,
        actor_id="user-1",
        action=action,
        resource_type="tenant",
        resource_id=resource_id,
        metadata=meta,
        created_at=NOW,
        previous_hash=previous_hash,
        event_hash=digest,
    )


# ---------------------------------------------------------------------------
# Metadata must never carry health values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "hrv_ms",
        "resting_hr",
        "sleep_min",
        "duration_min",
        "recovery_score",
        "steps",
        "temperature_deviation",
        "respiratory_rate",
        "session_load",
    ],
)
def test_health_shaped_keys_are_rejected(key: str) -> None:
    """An audit trail that logged measurements would be a PHI store."""
    with pytest.raises(InvalidAuditMetadataError, match="health value"):
        validate_metadata({key: "60"})


def test_ordinary_metadata_is_allowed() -> None:
    meta = {"outcome": "completed", "provider": "oura", "tool": "privacy.delete"}
    assert validate_metadata(meta) == meta


def test_rejection_is_not_silent_filtering() -> None:
    """A dropped key would make an incomplete record look complete."""
    with pytest.raises(InvalidAuditMetadataError):
        validate_metadata({"outcome": "ok", "hrv_ms": "62"})


def test_oversized_value_is_rejected() -> None:
    with pytest.raises(InvalidAuditMetadataError, match="exceeds"):
        validate_metadata({"note": "x" * 201})


def test_too_many_keys_is_rejected() -> None:
    with pytest.raises(InvalidAuditMetadataError, match="at most"):
        validate_metadata({f"k{n}": "v" for n in range(21)})


def test_returned_metadata_is_a_copy() -> None:
    """The caller must not be able to mutate what was validated."""
    original = {"outcome": "ok"}
    validated = validate_metadata(original)
    original["outcome"] = "tampered"
    assert validated == {"outcome": "ok"}


# ---------------------------------------------------------------------------
# The chain makes tampering detectable
# ---------------------------------------------------------------------------


def test_intact_chain_verifies() -> None:
    first = _event(event_id="e1", previous_hash=GENESIS_HASH)
    second = _event(event_id="e2", previous_hash=first.event_hash)

    assert verify_chain([first, second]) is None


def test_empty_chain_verifies() -> None:
    assert verify_chain([]) is None


def test_edited_event_is_detected() -> None:
    """Changing a stored row without recomputing its hash breaks verification."""
    first = _event(event_id="e1", previous_hash=GENESIS_HASH)
    second = _event(event_id="e2", previous_hash=first.event_hash)
    # Someone edits the first event's resource in the database.
    tampered = replace(first, resource_id="res-other")

    assert verify_chain([tampered, second]) == 0


def test_removed_event_is_detected() -> None:
    """Deleting a middle event orphans the link of the one that followed."""
    first = _event(event_id="e1", previous_hash=GENESIS_HASH)
    second = _event(event_id="e2", previous_hash=first.event_hash)
    third = _event(event_id="e3", previous_hash=second.event_hash)

    assert verify_chain([first, third]) == 1


def test_reordered_events_are_detected() -> None:
    first = _event(event_id="e1", previous_hash=GENESIS_HASH)
    second = _event(event_id="e2", previous_hash=first.event_hash)

    assert verify_chain([second, first]) == 0


def test_chain_cannot_be_truncated_by_nulling_the_link() -> None:
    """A genesis sentinel means a re-rooted chain is not silently valid.

    Dropping the first events and relabelling the survivor as the start still
    fails, because its stored ``previous_hash`` is not the genesis value.
    """
    first = _event(event_id="e1", previous_hash=GENESIS_HASH)
    second = _event(event_id="e2", previous_hash=first.event_hash)

    assert verify_chain([second]) == 0


def test_hash_depends_on_every_field() -> None:
    """Any content change must move the digest, or edits hide."""
    base: dict[str, object] = {
        "previous_hash": GENESIS_HASH,
        "tenant_id": "tenant-1",
        "actor_type": ActorType.USER,
        "actor_id": "user-1",
        "action": AuditAction.DELETE,
        "resource_type": "tenant",
        "resource_id": "res-1",
        "metadata": {"outcome": "ok"},
        "created_at": NOW,
    }
    baseline = chain_hash(**base)  # type: ignore[arg-type]

    variants: list[dict[str, object]] = [
        {"previous_hash": "1" * 64},
        {"tenant_id": "tenant-2"},
        {"actor_type": ActorType.WORKER},
        {"actor_id": "user-2"},
        {"action": AuditAction.EXPORT},
        {"resource_type": "connection"},
        {"resource_id": "res-2"},
        {"metadata": {"outcome": "failed"}},
        {"created_at": "2026-08-04T12:00:01Z"},
    ]
    for override in variants:
        assert chain_hash(**{**base, **override}) != baseline, override  # type: ignore[arg-type]


def test_metadata_key_order_does_not_change_the_hash() -> None:
    """Canonical JSON: the digest is a function of values, not serialization."""
    left = chain_hash(
        previous_hash=GENESIS_HASH,
        tenant_id="t",
        actor_type=ActorType.USER,
        actor_id=None,
        action=AuditAction.TOOL_INVOKE,
        resource_type="tool",
        resource_id="privacy.delete",
        metadata={"a": "1", "b": "2"},
        created_at=NOW,
    )
    right = chain_hash(
        previous_hash=GENESIS_HASH,
        tenant_id="t",
        actor_type=ActorType.USER,
        actor_id=None,
        action=AuditAction.TOOL_INVOKE,
        resource_type="tool",
        resource_id="privacy.delete",
        metadata={"b": "2", "a": "1"},
        created_at=NOW,
    )
    assert left == right


def test_non_string_value_is_rejected() -> None:
    """Values are stored as JSON strings; a number would round-trip wrong."""
    with pytest.raises(InvalidAuditMetadataError, match="must be a string"):
        validate_metadata({"count": 5})  # type: ignore[dict-item]
