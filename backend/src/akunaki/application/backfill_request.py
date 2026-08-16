"""Kick off a newly linked connection's history backfill.

This is the enqueue site for ``connection.initial_sync``. The job type and its
handler existed before this module did, but nothing in ``src/`` ever queued one
— only ``scripts/provider_sync.py``, a local development tool — so in a real
deployment a freshly linked connector's first data arrived whenever the
reconcile sweep next ran, up to its staleness cutoff later.

Why the initial job type rather than letting the incremental path cover it
--------------------------------------------------------------------------
``IncrementalSyncHandler`` does degrade to a full lookback when a stream has no
cursor, so backfill *would* eventually happen either way. Two things are lost
by leaning on that:

- the sync-run history records the trigger, and a first backfill recorded as
  ``schedule`` is indistinguishable from a routine refresh — the one run an
  operator most wants to find after a link is the one that would be mislabelled;
- ``InitialSyncHandler`` flips the connection to ``active`` and warns when a
  full-lookback backfill returns nothing, which is how an under-scoped OAuth
  grant becomes visible. Oura answers a scope-deficient token with an empty
  array rather than an error, so without that warning a silently empty
  connection looks exactly like a healthy one.

Fan-out mirrors the reconcile sweep: one job per stream the provider serves,
because each stream keeps its own cursor and retry budget, so a stream that is
failing (or that this account has no data for) cannot stop the others. The
enqueue is idempotency-keyed per connection and stream, so re-consenting to a
connector already linked does not stack a second backfill on top of one in
flight.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from akunaki.application.sync_handlers import streams_for_provider
from akunaki.domain.jobs import INITIAL_SYNC_JOB_TYPE
from akunaki.ports.jobs import JobEnqueuePort

logger = logging.getLogger("akunaki.backfill_request")

__all__ = ["BackfillOutcome", "BackfillRequestService"]


@dataclass(frozen=True, slots=True)
class BackfillOutcome:
    """What requesting a backfill produced."""

    job_ids: tuple[str, ...]
    """One job per stream, whether newly created or already in flight."""

    created: int
    """How many were newly queued; the rest deduplicated onto existing jobs."""


class BackfillRequestService:
    """Enqueue the initial history backfill for a freshly linked connection."""

    def __init__(
        self,
        *,
        jobs: JobEnqueuePort,
        new_id: Callable[[], str],
    ) -> None:
        self._jobs = jobs
        self._new_id = new_id

    def request_backfill(
        self,
        *,
        tenant_id: str,
        connection_id: str,
        provider: str,
        now: datetime,
    ) -> BackfillOutcome:
        """Queue one initial sync per stream ``provider`` serves.

        An unwired provider yields **no jobs rather than an error**: the link
        itself succeeded and the credentials are stored, so failing here would
        report a broken link over a working one. The reconcile sweep still
        reaches the connection once it is stale, so the backfill is delayed,
        not lost.
        """
        try:
            streams = streams_for_provider(provider)
        except KeyError:
            logger.warning(
                "linked provider has no backfill streams; leaving it to the sweep",
                extra={"connection_id": connection_id, "provider": provider},
            )
            return BackfillOutcome(job_ids=(), created=0)

        job_ids: list[str] = []
        created = 0
        for stream, _schema in streams:
            outcome = self._jobs.enqueue_job(
                job_id=self._new_id(),
                tenant_id=tenant_id,
                job_type=INITIAL_SYNC_JOB_TYPE,
                payload_json=json.dumps(
                    {"connection_id": connection_id, "stream": stream}, sort_keys=True
                ),
                now=now,
                # Per connection *and* stream: keying on the connection alone
                # would let the first stream's job suppress every other stream,
                # exactly as it would in the reconcile sweep.
                idempotency_key=f"initial_sync:{connection_id}:{stream}",
            )
            job_ids.append(outcome.job_id)
            if outcome.created:
                created += 1

        logger.info(
            "initial backfill requested",
            extra={
                "connection_id": connection_id,
                "provider": provider,
                "streams": len(streams),
                "created": created,
            },
        )
        return BackfillOutcome(job_ids=tuple(job_ids), created=created)
