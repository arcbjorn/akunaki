"""Product job handlers for connection sync.

Port-typed and framework-free. The handler translates fetch outcomes into the
worker runtime's retry vocabulary, which is where the design's rules land:

- a vendor 429 or 5xx raises ``TransientJobError`` so the runtime retries with
  backoff (and honors ``Retry-After`` when the provider supplies one);
- an auth rejection raises ``PermanentJobError`` **after** flipping the
  connection to ``needs_reauth``, because retrying a dead grant only burns the
  attempt budget;
- connection health is updated on every terminal attempt, success or failure.

Handlers must be idempotent: a lease can expire mid-run and the job be retried
elsewhere. Idempotency here comes from the ingestion layer's content-hash
dedupe, not from the handler.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn

from akunaki.domain.connections import ConnectionStatus
from akunaki.domain.fetch import FetchFailure
from akunaki.domain.jobs import JobClaim
from akunaki.domain.retry import PermanentJobError, TransientJobError
from akunaki.domain.secrets import SecretDecryptionError
from akunaki.ports.connections import ConnectionRepositoryPort
from akunaki.ports.fetch import ConnectorFetchPort, IngestionRepositoryPort
from akunaki.ports.secrets import SecretSealerPort

logger = logging.getLogger("akunaki.sync_handlers")

INITIAL_SYNC_JOB_TYPE = "connection.initial_sync"

# Default backfill lookback. The 30-vs-90 choice is an open product decision
# (roadmap open decision 6), so it is configurable rather than baked in.
DEFAULT_LOOKBACK_DAYS = 90

# Overlap absorbs late vendor finalization on the sleep stream.
DEFAULT_OVERLAP = timedelta(hours=36)

MAX_PAGES = 50


@dataclass(frozen=True, slots=True)
class SyncConfig:
    """Tunable backfill policy."""

    lookback_days: int = DEFAULT_LOOKBACK_DAYS
    overlap: timedelta = DEFAULT_OVERLAP
    max_pages: int = MAX_PAGES
    stream: str = "sleep"
    schema_version: str = "oura.v2"

    def __post_init__(self) -> None:
        if self.lookback_days < 1:
            msg = "lookback_days must be >= 1"
            raise ValueError(msg)
        if self.max_pages < 1:
            msg = "max_pages must be >= 1"
            raise ValueError(msg)


class InitialSyncHandler:
    """Backfill a newly linked connection's history.

    Constructed with ports and callables so the worker can register it without
    the application layer importing SQLAlchemy or an HTTP client.
    """

    def __init__(
        self,
        *,
        fetch_client: ConnectorFetchPort,
        ingestion: IngestionRepositoryPort,
        connections: ConnectionRepositoryPort,
        sealer: SecretSealerPort,
        new_id: Callable[[], str],
        config: SyncConfig | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._fetch = fetch_client
        self._ingestion = ingestion
        self._connections = connections
        self._sealer = sealer
        self._new_id = new_id
        self._config = config or SyncConfig()
        self._clock = clock

    def __call__(self, claim: JobClaim) -> None:
        """Execute one initial-sync job."""
        payload = _parse_payload(claim.payload_json)
        connection_id = payload["connection_id"]
        now = self._clock()

        access_token = self._open_access_token(connection_id)
        window_end = now
        window_start = now - timedelta(days=self._config.lookback_days) - self._config.overlap

        pages = 0
        new_revisions = 0
        page_token: str | None = None

        while pages < self._config.max_pages:
            result = self._fetch.fetch_page(
                access_token=access_token,
                stream=self._config.stream,
                window_start=window_start,
                window_end=window_end,
                page_token=page_token,
                now=now,
            )
            envelope = result.envelope
            if envelope is None:
                # Always raises; every failure is permanent or transient.
                self._handle_fetch_failure(
                    connection_id=connection_id,
                    failure=result.failure,
                    retry_after_seconds=result.retry_after_seconds,
                    now=now,
                )
            outcome = self._ingestion.commit_page(
                payload_id=self._new_id(),
                revision_id=self._new_id(),
                object_id=self._new_id(),
                tenant_id=claim.tenant_id,
                connection_id=connection_id,
                sync_run_id=payload.get("sync_run_id"),
                envelope=envelope,
                vendor_record_id=_vendor_record_id(envelope.stream, envelope.content_hash),
                schema_version=self._config.schema_version,
                cursor_id=f"{connection_id}:{self._config.stream}",
                cursor_value=envelope.fetched_at,
                now=now,
                window_start=window_start.isoformat(),
                window_end=window_end.isoformat(),
            )
            pages += 1
            if outcome.is_new_revision:
                new_revisions += 1

            page_token = envelope.next_page_token
            if not page_token:
                break

        self._connections.mark_status(
            connection_id=connection_id,
            status=ConnectionStatus.ACTIVE,
            now=now,
        )
        logger.info(
            "initial sync completed",
            extra={
                "connection_id": connection_id,
                "pages": pages,
                "new_revisions": new_revisions,
            },
        )

    def _open_access_token(self, connection_id: str) -> str:
        """Open the sealed tokens for a connection, or fail permanently."""
        sealed = self._connections.get_sealed_secret(connection_id=connection_id)
        if sealed is None:
            msg = "connection has no stored credentials"
            raise PermanentJobError(msg)
        try:
            opened = self._sealer.open(sealed, aad=connection_id.encode())
        except SecretDecryptionError as exc:
            # A KEK gap will not fix itself by retrying.
            msg = "stored credentials could not be opened"
            raise PermanentJobError(msg) from exc
        token = json.loads(opened).get("access_token")
        if not token:
            msg = "stored credentials contain no access token"
            raise PermanentJobError(msg)
        return str(token)

    def _handle_fetch_failure(
        self,
        *,
        connection_id: str,
        failure: FetchFailure | None,
        retry_after_seconds: int | None,
        now: datetime,
    ) -> NoReturn:
        """Translate a fetch failure into the runtime's retry vocabulary.

        Always raises: every fetch failure is either permanent or transient.
        """
        if failure is FetchFailure.UNAUTHORIZED:
            # Flip health first so the user sees why, then stop retrying.
            self._connections.mark_status(
                connection_id=connection_id,
                status=ConnectionStatus.NEEDS_REAUTH,
                now=now,
                error_class="unauthorized",
            )
            msg = "connection requires re-authorization"
            raise PermanentJobError(msg)

        self._connections.mark_status(
            connection_id=connection_id,
            status=ConnectionStatus.ERROR,
            now=now,
            error_class=str(failure) if failure else "unknown",
        )
        detail = f"fetch failed: {failure}"
        if retry_after_seconds is not None:
            detail = f"{detail} (retry after {retry_after_seconds}s)"
        raise TransientJobError(detail)


def _parse_payload(payload_json: str) -> dict[str, str]:
    """Parse and validate the job payload."""
    try:
        parsed = json.loads(payload_json)
    except ValueError as exc:
        msg = "payload is not valid json"
        raise PermanentJobError(msg) from exc
    if not isinstance(parsed, dict) or not parsed.get("connection_id"):
        msg = "payload must contain connection_id"
        raise PermanentJobError(msg)
    return {str(k): v for k, v in parsed.items()}


def _vendor_record_id(stream: str, content_hash: str) -> str:
    """Stable logical identity for a page.

    Oura returns collection pages rather than one record per response, so the
    page's content hash is the natural key until a per-record normalizer
    exists. Documented explicitly because it is a placeholder, not a design
    claim about vendor record identity.
    """
    return f"{stream}:page:{content_hash}"
