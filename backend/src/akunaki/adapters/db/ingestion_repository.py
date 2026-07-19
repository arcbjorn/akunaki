"""Atomic fetch commit: transport page, logical revision, and cursor.

One transaction per fetched page, exactly as the ingestion design requires:

- the transport row is **always** written (every vendor response is retained,
  including identical bodies on a retry);
- a logical revision is appended **only** when that object has not already
  recorded this ``content_hash``;
- the cursor advances in the same transaction.

A crash before commit therefore leaves the cursor unchanged, so the same
window is safely refetched, and the hash check stops the retry from creating a
duplicate revision.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.models import Job, RawObject, RawPayload, RawRevision, SyncCursor
from akunaki.domain.fetch import RawEnvelope
from akunaki.domain.jobs import (
    NORMALIZE_JOB_TYPE,
    JobRole,
    JobStatus,
    require_aware,
    to_utc_rfc3339,
)
from akunaki.ports.facts import RevisionBody


@dataclass(frozen=True, slots=True)
class CommitOutcome:
    """What one atomic page commit actually persisted."""

    payload_id: str
    revision_id: str | None
    revision_n: int | None
    is_new_revision: bool
    normalize_job_id: str | None = None


class IngestionRepository:
    """Persist fetched pages and their logical revisions atomically."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def commit_page(
        self,
        *,
        payload_id: str,
        revision_id: str,
        object_id: str,
        tenant_id: str,
        connection_id: str,
        sync_run_id: str | None,
        envelope: RawEnvelope,
        vendor_record_id: str,
        schema_version: str,
        cursor_id: str,
        cursor_value: str,
        now: datetime,
        window_start: str | None = None,
        window_end: str | None = None,
        normalize_job_id: str | None = None,
    ) -> CommitOutcome:
        """Commit one fetched page: transport row, logical revision, cursor.

        When ``normalize_job_id`` is supplied and the revision is genuinely
        new, a ``raw.normalize`` job is enqueued in the **same transaction**.
        """
        for name, value in (
            ("payload_id", payload_id),
            ("tenant_id", tenant_id),
            ("connection_id", connection_id),
            ("vendor_record_id", vendor_record_id),
        ):
            if not value:
                msg = f"{name} must be non-empty"
                raise ValueError(msg)

        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))

        with self._session_factory() as session, session.begin():
            # 1. Transport row: always written, never deduped.
            session.add(
                RawPayload(
                    id=payload_id,
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    sync_run_id=sync_run_id,
                    transport_kind="sync_fetch",
                    provider=envelope.provider,
                    stream=envelope.stream,
                    page_token=envelope.page_token,
                    fetched_at=envelope.fetched_at,
                    received_at=now_s,
                    http_status=envelope.http_status,
                    content_type=envelope.content_type,
                    content_hash=envelope.content_hash,
                    payload_json=envelope.payload_text,
                    payload_blob=None,
                    request_meta_json=json.dumps(envelope.request_meta, sort_keys=True),
                )
            )

            # 2. Logical object identity (created on first sight).
            existing_object = session.execute(
                select(RawObject).where(
                    RawObject.tenant_id == tenant_id,
                    RawObject.provider == envelope.provider,
                    RawObject.stream == envelope.stream,
                    RawObject.vendor_record_id == vendor_record_id,
                )
            ).scalar_one_or_none()
            if existing_object is None:
                existing_object = RawObject(
                    id=object_id,
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    provider=envelope.provider,
                    stream=envelope.stream,
                    vendor_record_id=vendor_record_id,
                    current_revision_id=None,
                    created_at=now_s,
                )
                session.add(existing_object)
                session.flush()
            resolved_object_id = existing_object.id

            # 3. Logical revision: appended only when content is genuinely new.
            already_seen = session.execute(
                select(RawRevision.id).where(
                    RawRevision.raw_object_id == resolved_object_id,
                    RawRevision.content_hash == envelope.content_hash,
                    RawRevision.is_tombstone == 0,
                )
            ).first()

            new_revision_id: str | None = None
            new_revision_n: int | None = None
            if already_seen is None:
                highest = session.execute(
                    select(func.max(RawRevision.revision_n)).where(
                        RawRevision.raw_object_id == resolved_object_id
                    )
                ).scalar()
                new_revision_n = (highest or 0) + 1
                new_revision_id = revision_id
                session.add(
                    RawRevision(
                        id=revision_id,
                        tenant_id=tenant_id,
                        raw_object_id=resolved_object_id,
                        raw_payload_id=payload_id,
                        sync_run_id=sync_run_id,
                        revision_n=new_revision_n,
                        vendor_record_id=vendor_record_id,
                        observed_at=None,
                        effective_at=None,
                        received_at=now_s,
                        content_hash=envelope.content_hash,
                        schema_version=schema_version,
                        deletion_state="active",
                        is_tombstone=0,
                        tombstone_reason=None,
                    )
                )
                existing_object.current_revision_id = revision_id

                # 3b. Normalization job, same transaction as the revision it
                # describes. This is the design's "outbox rows (or jobs)": a
                # crash after commit leaves the job durable, and a crash before
                # commit leaves neither, so normalization can never be silently
                # skipped for a revision that exists.
                if normalize_job_id is not None:
                    session.add(
                        Job(
                            id=normalize_job_id,
                            tenant_id=tenant_id,
                            role=JobRole.CORE.value,
                            status=JobStatus.READY.value,
                            payload_json=json.dumps(
                                {
                                    "raw_revision_id": revision_id,
                                    "raw_payload_id": payload_id,
                                },
                                sort_keys=True,
                            ),
                            priority=100,
                            run_after=now_s,
                            attempts=0,
                            max_attempts=5,
                            # Keyed by revision: a redelivered enqueue for the
                            # same revision dedupes rather than fanning out.
                            idempotency_key=f"normalize:{revision_id}",
                            fence_token=0,
                            created_at=now_s,
                            updated_at=now_s,
                            job_type=NORMALIZE_JOB_TYPE,
                        )
                    )

            # 4. Cursor advance, same transaction as the data it describes.
            session.merge(
                SyncCursor(
                    id=cursor_id,
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    stream=envelope.stream,
                    cursor_type="timestamp",
                    cursor_value=cursor_value,
                    window_start=window_start,
                    window_end=window_end,
                    updated_at=now_s,
                )
            )

            return CommitOutcome(
                payload_id=payload_id,
                revision_id=new_revision_id,
                revision_n=new_revision_n,
                is_new_revision=new_revision_id is not None,
                normalize_job_id=normalize_job_id if new_revision_id is not None else None,
            )

    def get_cursor(self, *, connection_id: str, stream: str) -> str | None:
        """Return the stored cursor value for a stream, if any."""
        with self._session_factory() as session:
            return session.execute(
                select(SyncCursor.cursor_value).where(
                    SyncCursor.connection_id == connection_id,
                    SyncCursor.stream == stream,
                )
            ).scalar_one_or_none()


class RevisionReader:
    """Read immutable raw revisions joined to their exact transport body."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get_revision(self, *, revision_id: str) -> RevisionBody | None:
        """Return the revision and its body, or None when unknown."""
        if not revision_id:
            return None
        with self._session_factory() as session:
            row = session.execute(
                select(
                    RawRevision.id,
                    RawRevision.raw_payload_id,
                    RawRevision.schema_version,
                    RawRevision.is_tombstone,
                    RawPayload.payload_json,
                    RawPayload.connection_id,
                )
                .join(RawPayload, RawPayload.id == RawRevision.raw_payload_id)
                .where(RawRevision.id == revision_id)
            ).one_or_none()
            if row is None:
                return None
            (
                found_id,
                payload_id,
                schema_version,
                is_tombstone,
                payload_json,
                connection_id,
            ) = row
            return RevisionBody(
                revision_id=found_id,
                connection_id=connection_id,
                raw_payload_id=payload_id,
                schema_version=schema_version,
                # A tombstone may legitimately carry an empty body.
                payload_text=payload_json or "",
                is_tombstone=bool(is_tombstone),
            )
