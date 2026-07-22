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
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn

from akunaki.domain.connections import ConnectionStatus
from akunaki.domain.fetch import FetchFailure
from akunaki.domain.jobs import (
    INITIAL_SYNC_JOB_TYPE,
    NORMALIZE_JOB_TYPE,
    SCORE_RECOMPUTE_JOB_TYPE,
    JobClaim,
)
from akunaki.domain.record_split import split_page
from akunaki.domain.retry import PermanentJobError, TransientJobError
from akunaki.domain.secrets import SecretDecryptionError
from akunaki.domain.sleep_normalizer import NormalizationError, normalize_sleep_payload
from akunaki.domain.vitals_normalizer import normalize_vitals_payload
from akunaki.domain.workout_normalizer import normalize_workout_payload
from akunaki.ports.connections import ConnectionRepositoryPort
from akunaki.ports.facts import FactWriterPort, RevisionBody, RevisionReaderPort
from akunaki.ports.fetch import ConnectorFetchPort, IngestionRepositoryPort
from akunaki.ports.jobs import JobRepositoryPort
from akunaki.ports.secrets import SecretSealerPort

logger = logging.getLogger("akunaki.sync_handlers")

# Re-exported from domain so callers can register handlers without reaching
# into akunaki.domain.jobs for a job-type string.
__all__ = [
    "INITIAL_SYNC_JOB_TYPE",
    "NORMALIZE_JOB_TYPE",
    "InitialSyncHandler",
    "NormalizeHandler",
    "SyncConfig",
]


# Default backfill lookback. Roadmap decision 6 (2026-07-19) settled on 30 days
# for lower first-sync cost and vendor load; still configurable per connection.
DEFAULT_LOOKBACK_DAYS = 30

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
            # Split the page into per-record slices: raw identity is per
            # record, not per response page.
            records = split_page(envelope.stream, envelope.payload_text)
            outcome = self._ingestion.commit_page(
                payload_id=self._new_id(),
                records=records,
                ids=_id_stream(self._new_id),
                tenant_id=claim.tenant_id,
                connection_id=connection_id,
                sync_run_id=payload.get("sync_run_id"),
                envelope=envelope,
                schema_version=self._config.schema_version,
                cursor_id=f"{connection_id}:{self._config.stream}",
                cursor_value=envelope.fetched_at,
                now=now,
                window_start=window_start.isoformat(),
                window_end=window_end.isoformat(),
            )
            pages += 1
            new_revisions += len(outcome.new_revision_ids)

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


def _id_stream(new_id: Callable[[], str]) -> Iterator[str]:
    """Unbounded id supply for one page commit."""
    while True:
        yield new_id()


class NormalizeHandler:
    """Normalize one raw revision into canonical facts.

    Keyed by ``raw_revision_id``, so a retry re-reads the same immutable raw
    row and produces the same facts. Idempotency comes from the fact layer:
    identical normalized content writes no new version.
    """

    def __init__(
        self,
        *,
        revisions: RevisionReaderPort,
        facts: FactWriterPort,
        jobs: JobRepositoryPort,
        new_id: Callable[[], str],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._revisions = revisions
        self._facts = facts
        self._jobs = jobs
        self._new_id = new_id
        self._clock = clock

    def __call__(self, claim: JobClaim) -> None:
        """Execute one normalization job."""
        payload = _parse_normalize_payload(claim.payload_json)
        revision_id = payload["raw_revision_id"]
        now = self._clock()

        revision = self._revisions.get_revision(revision_id=revision_id)
        if revision is None:
            # The revision is immutable and should exist; a missing one means a
            # stale or malformed job, not a transient condition.
            msg = "raw revision not found"
            raise PermanentJobError(msg)

        if revision.is_tombstone:
            # Vendor deletions are handled by the deletion path, not by
            # normalizing an empty body into a fact.
            logger.info(
                "skipping tombstone revision",
                extra={"raw_revision_id": revision_id},
            )
            return

        # Dispatch by the revision's schema version: an Oura sleep page yields
        # sleep + overnight-vitals facts; a Polar exercise page yields workout
        # (zone-load) facts. Unknown schemas parse nothing and write nothing.
        written = 0
        affected_days: set[str] = set()
        try:
            if revision.schema_version.startswith("polar."):
                affected_days = self._normalize_workouts(claim, revision, now)
            else:
                sleep_facts = normalize_sleep_payload(revision.payload_text)
                # Overnight vitals ride along on the same sleep payload; one page
                # yields both the sleep and the HRV/RHR facts, with shared lineage.
                vitals_facts = normalize_vitals_payload(revision.payload_text)
                for fact in sleep_facts:
                    outcome = self._facts.write_sleep_fact(
                        fact_record_id=self._new_id(),
                        tenant_id=claim.tenant_id,
                        connection_id=revision.connection_id,
                        fact=fact,
                        raw_revision_id=revision_id,
                        raw_payload_id=revision.raw_payload_id,
                        schema_version=revision.schema_version,
                        now=now,
                    )
                    if outcome.is_new_version:
                        written += 1
                for vitals in vitals_facts:
                    outcome = self._facts.write_vitals_fact(
                        fact_record_id=self._new_id(),
                        tenant_id=claim.tenant_id,
                        connection_id=revision.connection_id,
                        fact=vitals,
                        raw_revision_id=revision_id,
                        raw_payload_id=revision.raw_payload_id,
                        schema_version=revision.schema_version,
                        now=now,
                    )
                    if outcome.is_new_version:
                        written += 1
                affected_days = {f.local_health_day for f in sleep_facts} | {
                    f.local_health_day for f in vitals_facts
                }
        except NormalizationError as exc:
            # A body that cannot be parsed will not parse on retry either.
            msg = "raw payload could not be normalized"
            raise PermanentJobError(msg) from exc

        # Chain a score recompute for each affected local health day. One
        # revision carries one record (per-record slicing), so this is normally
        # a single day. Keyed by revision, so a normalize retry does not stack
        # duplicate recomputes, while a *new* revision (a correction) enqueues a
        # fresh recompute. Enqueued regardless of ``written``: a re-normalization
        # that produced no new fact version still safely dedupes at the score
        # layer, and enqueuing is cheap and idempotent.
        for day in sorted(affected_days):
            self._jobs.enqueue_job(
                job_id=self._new_id(),
                tenant_id=claim.tenant_id,
                job_type=SCORE_RECOMPUTE_JOB_TYPE,
                payload_json=json.dumps({"local_health_day": day}, sort_keys=True),
                now=now,
                idempotency_key=f"recompute:{revision_id}:{day}",
            )

        logger.info(
            "normalized raw revision",
            extra={
                "raw_revision_id": revision_id,
                "schema_version": revision.schema_version,
                "versions_written": written,
            },
        )

    def _normalize_workouts(
        self,
        claim: JobClaim,
        revision: RevisionBody,
        now: datetime,
    ) -> set[str]:
        """Normalize a Polar exercise revision into workout facts."""
        facts = normalize_workout_payload(revision.payload_text)
        for fact in facts:
            self._facts.write_workout_fact(
                fact_record_id=self._new_id(),
                tenant_id=claim.tenant_id,
                connection_id=revision.connection_id,
                fact=fact,
                raw_revision_id=revision.revision_id,
                raw_payload_id=revision.raw_payload_id,
                schema_version=revision.schema_version,
                now=now,
            )
        return {fact.local_health_day for fact in facts}


def _parse_normalize_payload(payload_json: str) -> dict[str, str]:
    """Parse and validate a normalize job payload."""
    try:
        parsed = json.loads(payload_json)
    except ValueError as exc:
        msg = "payload is not valid json"
        raise PermanentJobError(msg) from exc
    if not isinstance(parsed, dict) or not parsed.get("raw_revision_id"):
        msg = "payload must contain raw_revision_id"
        raise PermanentJobError(msg)
    return {str(k): v for k, v in parsed.items()}
