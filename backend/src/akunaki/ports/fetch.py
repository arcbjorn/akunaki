"""Connector fetch port.

Adapters implement this protocol. Domain and ports must not import an HTTP
client, so the transport is an adapter concern only.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime
from typing import Protocol

from akunaki.domain.fetch import FetchResult, RawEnvelope
from akunaki.domain.record_split import RecordSlice


class ConnectorFetchPort(Protocol):
    """Fetch one page of vendor data for a stream and window."""

    @property
    def provider(self) -> str:
        """Provider identifier."""
        ...

    def fetch_page(
        self,
        *,
        access_token: str,
        stream: str,
        window_start: datetime,
        window_end: datetime,
        page_token: str | None,
        now: datetime,
    ) -> FetchResult:
        """Fetch one page. Never raises for provider or transport failures."""
        ...


class CommitOutcomeLike(Protocol):
    """What one atomic page commit persisted."""

    @property
    def new_revision_ids(self) -> tuple[str, ...]:
        """Revisions appended by this commit (one per genuinely-new record)."""
        ...

    @property
    def normalize_job_ids(self) -> tuple[str, ...]:
        """Normalization jobs enqueued alongside those revisions."""
        ...

    @property
    def is_new_revision(self) -> bool:
        """True when at least one revision was appended."""
        ...


class IngestionRepositoryPort(Protocol):
    """Persist fetched pages and their logical revisions atomically."""

    def commit_page(
        self,
        *,
        payload_id: str,
        records: Sequence[RecordSlice],
        ids: Iterator[str],
        tenant_id: str,
        connection_id: str,
        sync_run_id: str | None,
        envelope: RawEnvelope,
        schema_version: str,
        cursor_id: str,
        cursor_value: str,
        now: datetime,
        window_start: str | None = None,
        window_end: str | None = None,
    ) -> CommitOutcomeLike:
        """Commit one fetched page in a single transaction."""
        ...

    def get_cursor(self, *, connection_id: str, stream: str) -> str | None:
        """Return the stored cursor value for a stream, if any."""
        ...
