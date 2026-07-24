"""Webhook inbox persistence: durable, deduplicated delivery records.

A verified webhook delivery is recorded exactly once per
``(connection_id, dedupe_key)`` via an atomic ``INSERT ... ON CONFLICT DO
NOTHING``. A redelivery (vendor retry) collides on the unique key and is
recognized as a duplicate, so the caller acknowledges without re-processing.

The row is inserted with ``processing_status = 'accepted'``; a later step sets
it to ``enqueued`` once a refetch job is queued. Header metadata is stored as
**redacted** JSON — no signature or secret — so the inbox is safe to inspect.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.models import WebhookInbox
from akunaki.domain.jobs import require_aware, to_utc_rfc3339


@dataclass(frozen=True, slots=True)
class InboxRecord:
    """The outcome of recording a webhook delivery."""

    inbox_id: str
    is_duplicate: bool
    """True when this delivery collided with an already-recorded one."""


class WebhookInboxRepository:
    """Record and advance durable webhook deliveries."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def record_delivery(
        self,
        *,
        inbox_id: str,
        tenant_id: str,
        connection_id: str,
        provider: str,
        dedupe_key: str,
        delivery_id: str | None,
        headers_meta: dict[str, str],
        now: datetime,
    ) -> InboxRecord:
        """Insert a verified delivery, deduped on ``(connection_id, dedupe_key)``.

        Returns ``is_duplicate=True`` (and the pre-existing row's id) when the
        delivery was already recorded, so the caller can acknowledge a vendor
        redelivery without enqueuing a second refetch.
        """
        if not dedupe_key:
            msg = "dedupe_key must be non-empty"
            raise ValueError(msg)
        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))

        with self._session_factory() as session, session.begin():
            result = session.execute(
                sqlite_insert(WebhookInbox)
                .values(
                    id=inbox_id,
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    provider=provider,
                    delivery_id=delivery_id,
                    dedupe_key=dedupe_key,
                    received_at=now_s,
                    verified_at=now_s,
                    headers_meta_json=json.dumps(headers_meta, sort_keys=True),
                    body_payload_id=None,
                    processing_status="accepted",
                )
                .on_conflict_do_nothing(index_elements=["connection_id", "dedupe_key"])
            )
            if _affected_rows(result) == 1:
                return InboxRecord(inbox_id=inbox_id, is_duplicate=False)

            # A duplicate: find the existing row so the caller can reference it.
            existing_id = session.execute(
                select(WebhookInbox.id).where(
                    WebhookInbox.connection_id == connection_id,
                    WebhookInbox.dedupe_key == dedupe_key,
                )
            ).scalar_one_or_none()
            return InboxRecord(inbox_id=existing_id or inbox_id, is_duplicate=True)

    def mark_enqueued(self, *, inbox_id: str) -> bool:
        """Advance a delivery to ``enqueued`` after its refetch job is queued."""
        with self._session_factory() as session, session.begin():
            result = session.execute(
                update(WebhookInbox)
                .where(
                    WebhookInbox.id == inbox_id,
                    WebhookInbox.processing_status == "accepted",
                )
                .values(processing_status="enqueued")
            )
            return _affected_rows(result) == 1


def _affected_rows(result: object) -> int:
    """Rows affected by a Core statement (0 or 1 here)."""
    rowcount = getattr(result, "rowcount", 0)
    return int(rowcount) if isinstance(rowcount, int) else 0
