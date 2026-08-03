"""Confirmation bindings for mutating tool invocations.

A confirmation authorizes **one specific call**, not a tool in general. It is
one-time and expiring, bound to who is acting, what they are calling, and the
exact arguments they were shown — so a confirmation obtained for
"delete tenant A" can never execute "delete tenant B", and a model cannot
substitute arguments between the user's approval and execution.

Pure: no clock, no I/O. The caller supplies ``now`` so binding checks stay
deterministic and testable.

Why the args hash is canonical
------------------------------
The user approves arguments they were *shown*. If the hash depended on key
order or float formatting, an attacker could present one JSON encoding for
approval and submit a differently-encoded but equivalent payload — or worse,
the same payload could fail to match itself across a serialization round trip
and silently re-prompt. Canonical JSON (sorted keys, no incidental whitespace)
makes the hash a function of the *values*, so equivalent arguments always
produce the same binding and different arguments never collide.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

__all__ = [
    "ConfirmationBinding",
    "ConfirmationRejection",
    "ConfirmationStatus",
    "canonical_args_hash",
    "check_confirmation",
]


class ConfirmationStatus(StrEnum):
    """Lifecycle of one confirmation."""

    PENDING = "pending"
    """Issued and awaiting execution."""

    CONSUMED = "consumed"
    """Already used; a replay must fail."""

    CANCELLED = "cancelled"
    """Withdrawn before use."""


class ConfirmationRejection(StrEnum):
    """Why a confirmation did not authorize an execution.

    Deliberately coarse at the boundary: the API collapses these to one generic
    failure so a caller cannot learn *which* part of the binding mismatched,
    which would otherwise let someone probe for a valid tool name or run id.
    """

    UNKNOWN = "unknown"
    EXPIRED = "expired"
    ALREADY_CONSUMED = "already_consumed"
    CANCELLED = "cancelled"
    BINDING_MISMATCH = "binding_mismatch"


@dataclass(frozen=True, slots=True)
class ConfirmationBinding:
    """What a confirmation authorizes.

    Every field is part of the identity of the authorized call. Two invocations
    differing in any one of them are different calls and need separate
    confirmations.
    """

    tenant_id: str
    user_id: str
    run_id: str | None
    tool_name: str
    args_hash: str
    idempotency_key: str

    def matches(self, other: ConfirmationBinding) -> bool:
        """Whether ``other`` is the same authorized call.

        Compared field by field rather than by object identity so a stored
        binding rehydrated from the database compares equal to a freshly built
        one.
        """
        return (
            self.tenant_id == other.tenant_id
            and self.user_id == other.user_id
            and self.run_id == other.run_id
            and self.tool_name == other.tool_name
            and self.args_hash == other.args_hash
            and self.idempotency_key == other.idempotency_key
        )


def canonical_args_hash(args: dict[str, Any]) -> str:
    """SHA-256 over the canonical JSON encoding of a tool's arguments.

    Sorted keys and compact separators, so logically identical arguments hash
    identically regardless of how the client serialized them.

    Raises rather than coercing: an unserializable value raises ``TypeError``
    and a NaN/infinity raises ``ValueError``. A binding that silently
    stringified an unexpected type would authorize something other than what
    was approved, and NaN never compares equal to itself, so a NaN argument
    could never re-match its own binding.
    """
    material = json.dumps(args, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def check_confirmation(
    *,
    stored: ConfirmationBinding,
    status: ConfirmationStatus,
    expires_at: datetime,
    requested: ConfirmationBinding,
    now: datetime,
) -> ConfirmationRejection | None:
    """Return why the confirmation does not authorize ``requested``, or None.

    Order matters only for clarity of the returned reason; every check must
    pass. Expiry is checked as ``now >= expires_at`` so a confirmation is dead
    exactly at its deadline rather than one tick after.
    """
    if status is ConfirmationStatus.CONSUMED:
        # Rule 4: replay of a consumed confirmation fails.
        return ConfirmationRejection.ALREADY_CONSUMED
    if status is ConfirmationStatus.CANCELLED:
        return ConfirmationRejection.CANCELLED
    if now >= expires_at:
        return ConfirmationRejection.EXPIRED
    if not stored.matches(requested):
        # Rule 2: reauthorize against the same binding; arg substitution fails.
        return ConfirmationRejection.BINDING_MISMATCH
    return None
