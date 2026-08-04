"""Audit persistence: append to the chain, and verify it.

Appending is not a plain insert. The new event's hash depends on the previous
one, so reading the tail and inserting must be **one** transaction — two
concurrent writers that both read the same tail would compute the same
``previous_hash`` and fork the chain into two branches that each look valid
alone. The unique constraint on ``event_hash`` is the backstop: identical
content at the same position collides rather than silently duplicating.
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.models import AuditEventRow
from akunaki.domain.audit import (
    GENESIS_HASH,
    ActorType,
    AuditAction,
    AuditEvent,
    chain_hash,
    validate_metadata,
    verify_link,
)
from akunaki.domain.jobs import require_aware, to_utc_rfc3339

# Bounded read per round trip: big enough to be few queries, small enough
# that one batch never dominates worker memory.
_VERIFY_BATCH = 500

__all__ = ["AuditRepository"]


class AuditRepository:
    """Append tamper-evident audit events and verify the stored chain."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def record(
        self,
        *,
        event_id: str,
        tenant_id: str | None,
        actor_type: ActorType,
        actor_id: str | None,
        action: AuditAction,
        resource_type: str,
        resource_id: str | None,
        metadata: dict[str, str],
        now: datetime,
    ) -> str:
        """Append one event and return its hash.

        Metadata is validated **before** the transaction opens: a record that
        would carry a health value must fail at the call site, not leave a
        half-open transaction behind.
        """
        safe_metadata = validate_metadata(metadata)
        created_at = to_utc_rfc3339(require_aware(now, field_name="now"))

        with self._session_factory() as session, session.begin():
            # Read the tail and append in one transaction: a concurrent writer
            # that read the same tail would otherwise fork the chain.
            previous = session.execute(
                select(AuditEventRow.event_hash).order_by(AuditEventRow.seq.desc()).limit(1)
            ).scalar_one_or_none()
            previous_hash = previous if previous is not None else GENESIS_HASH

            digest = chain_hash(
                previous_hash=previous_hash,
                tenant_id=tenant_id,
                actor_type=actor_type,
                actor_id=actor_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                metadata=safe_metadata,
                created_at=created_at,
            )
            session.add(
                AuditEventRow(
                    id=event_id,
                    tenant_id=tenant_id,
                    actor_type=actor_type.value,
                    actor_id=actor_id,
                    action=action.value,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    metadata_json=json.dumps(safe_metadata, sort_keys=True),
                    created_at=created_at,
                    previous_hash=previous_hash,
                    event_hash=digest,
                )
            )
            return digest

    def verify(self, *, batch_size: int = _VERIFY_BATCH) -> int | None:
        """Return the ``seq`` of the first tampered event, or None when intact.

        Walks the chain in insertion order **in batches**: the audit table only
        grows, so materializing it would eventually exhaust memory on the very
        deployment that has the most history to protect.

        The chain is global rather than per-tenant so a deleted tenant's events
        still anchor the ones that followed — a per-tenant chain would let
        erasing one tenant silently re-root another's history.
        """
        if batch_size < 1:
            msg = "batch_size must be >= 1"
            raise ValueError(msg)

        expected_previous = GENESIS_HASH
        after_seq = 0
        while True:
            with self._session_factory() as session:
                rows = list(
                    session.execute(
                        select(AuditEventRow)
                        .where(AuditEventRow.seq > after_seq)
                        .order_by(AuditEventRow.seq)
                        .limit(batch_size)
                    ).scalars()
                )
            if not rows:
                return None

            for row in rows:
                if not verify_link(_to_event(row), expected_previous=expected_previous):
                    return row.seq
                expected_previous = row.event_hash
            after_seq = rows[-1].seq

    def tail(self) -> tuple[int, str] | None:
        """Return ``(seq, created_at)`` of the newest event, or None when empty.

        O(1) on the ``seq`` primary key, so an operational probe can report how
        current the trail is without the O(chain) walk verification needs.
        """
        with self._session_factory() as session:
            row = session.execute(
                select(AuditEventRow.seq, AuditEventRow.created_at)
                .order_by(AuditEventRow.seq.desc())
                .limit(1)
            ).first()
        return (row[0], row[1]) if row is not None else None


def _to_event(row: AuditEventRow) -> AuditEvent:
    """Rehydrate a stored row into the domain event the verifier checks."""
    return AuditEvent(
        event_id=row.id,
        tenant_id=row.tenant_id,
        actor_type=ActorType(row.actor_type),
        actor_id=row.actor_id,
        action=AuditAction(row.action),
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        metadata=json.loads(row.metadata_json),
        created_at=row.created_at,
        previous_hash=row.previous_hash,
        event_hash=row.event_hash,
    )
