"""Audit events: what happened, to what, by whom — never health values.

Audit answers **repudiation** ("I didn't delete that"), which means the record
has to be trustworthy in two ways the ordinary tables are not:

1. **It must not become a second copy of the health data.** An audit trail that
   logged what a score *said* would be a PHI store with a longer retention
   policy and looser access. So metadata is a bounded key/value map validated
   here, and any value that looks like a health measurement is rejected at
   construction rather than filtered at read time.

2. **It must be tamper-evident.** Each event carries the hash of the one before
   it, so removing or editing a past event breaks every link after it. This is
   a hash chain, not a signature: it detects tampering by someone who edits
   rows, and does **not** defend against an attacker who can rewrite the whole
   chain. That limit is real and worth stating rather than implying more.

Pure: no clock, no I/O. The caller supplies ``now`` and the previous hash.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "AUDIT_CHAIN_CHECK",
    "GENESIS_HASH",
    "ActorType",
    "AuditAction",
    "AuditEvent",
    "InvalidAuditMetadataError",
    "chain_hash",
    "verify_chain",
]

# The "previous hash" of the first event in a tenant's chain. A fixed sentinel
# rather than empty string, so a truncated chain cannot be passed off as a fresh
# one by nulling the link.
GENESIS_HASH: Final = "0" * 64

# Name of the scheduled audit-chain verification, shared by the handler that
# writes the verdict and the probe that reads it.
AUDIT_CHAIN_CHECK: Final = "audit_chain"


class ActorType(StrEnum):
    """Who performed the action."""

    USER = "user"
    SYSTEM = "system"
    WORKER = "worker"


class AuditAction(StrEnum):
    """The audited action vocabulary.

    Deliberately closed: an open string would let a caller invent an action name
    that no reviewer knows to look for.
    """

    CONNECTION_CREATE = "connection.create"
    CONNECTION_SYNC = "connection.sync"
    CONNECTION_REVOKE = "connection.revoke"
    TOOL_INVOKE = "tool.invoke"
    EXPORT = "export"
    DELETE = "delete"


class InvalidAuditMetadataError(ValueError):
    """Metadata carried a value an audit record must never hold."""


# Metadata keys that would smuggle a health measurement into the audit trail.
# Matched on the key, because the point is to refuse the *shape* of a health
# record rather than guess whether a given number is one.
_FORBIDDEN_KEY_PARTS: Final = (
    "hrv",
    "heart_rate",
    "resting_hr",
    "sleep_min",
    "duration_min",
    "score",
    "steps",
    "temperature",
    "respiratory",
    "load",
)

_MAX_METADATA_KEYS: Final = 20
_MAX_VALUE_LENGTH: Final = 200


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One recorded action, ready to persist.

    ``tenant_id`` is None for system-scoped actions (a scheduled sweep belongs
    to no customer). ``metadata`` is validated PHI-free at construction.
    """

    event_id: str
    tenant_id: str | None
    actor_type: ActorType
    actor_id: str | None
    action: AuditAction
    resource_type: str
    resource_id: str | None
    metadata: dict[str, str]
    created_at: str
    previous_hash: str
    event_hash: str


def validate_metadata(metadata: dict[str, str]) -> dict[str, str]:
    """Return ``metadata`` if it is safe to audit, else raise.

    Rejects rather than sanitizes: silently dropping a forbidden key would make
    an audit record that looks complete while omitting what the caller meant to
    record, which is worse than a loud failure at the call site.
    """
    if len(metadata) > _MAX_METADATA_KEYS:
        msg = f"audit metadata may carry at most {_MAX_METADATA_KEYS} keys"
        raise InvalidAuditMetadataError(msg)

    for key, value in metadata.items():
        lowered = key.lower()
        for part in _FORBIDDEN_KEY_PARTS:
            if part in lowered:
                msg = f"audit metadata key {key!r} looks like a health value"
                raise InvalidAuditMetadataError(msg)
        if not isinstance(value, str):
            msg = f"audit metadata value for {key!r} must be a string"
            raise InvalidAuditMetadataError(msg)
        if len(value) > _MAX_VALUE_LENGTH:
            msg = f"audit metadata value for {key!r} exceeds {_MAX_VALUE_LENGTH} characters"
            raise InvalidAuditMetadataError(msg)
    return dict(metadata)


def chain_hash(
    *,
    previous_hash: str,
    tenant_id: str | None,
    actor_type: ActorType,
    actor_id: str | None,
    action: AuditAction,
    resource_type: str,
    resource_id: str | None,
    metadata: dict[str, str],
    created_at: str,
) -> str:
    """SHA-256 over the event's content **and** the previous event's hash.

    Canonical JSON (sorted keys, compact separators) so the digest is a function
    of the values, not of how they were serialized. Including
    ``previous_hash`` is what makes the chain: editing any earlier event changes
    every hash after it.
    """
    material = json.dumps(
        {
            "previous_hash": previous_hash,
            "tenant_id": tenant_id,
            "actor_type": actor_type.value,
            "actor_id": actor_id,
            "action": action.value,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "metadata": metadata,
            "created_at": created_at,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def verify_link(event: AuditEvent, *, expected_previous: str) -> bool:
    """Whether ``event`` links to ``expected_previous`` and matches its own hash.

    The single-event step behind :func:`verify_chain`, exposed so a caller can
    walk a long chain in batches without holding every event in memory — the
    audit table only grows, so a verifier that materializes it does not survive
    contact with a real deployment.
    """
    if event.previous_hash != expected_previous:
        return False
    recomputed = chain_hash(
        previous_hash=event.previous_hash,
        tenant_id=event.tenant_id,
        actor_type=event.actor_type,
        actor_id=event.actor_id,
        action=event.action,
        resource_type=event.resource_type,
        resource_id=event.resource_id,
        metadata=event.metadata,
        created_at=event.created_at,
    )
    return recomputed == event.event_hash


def verify_chain(events: list[AuditEvent]) -> int | None:
    """Return the index of the first tampered event, or None when intact.

    ``events`` must be in insertion order. Checks both that each event's own
    hash matches its content (detecting an edited row) and that it links to its
    predecessor (detecting a removed or reordered one).
    """
    expected_previous = GENESIS_HASH
    for index, event in enumerate(events):
        if not verify_link(event, expected_previous=expected_previous):
            return index
        expected_previous = event.event_hash
    return None
