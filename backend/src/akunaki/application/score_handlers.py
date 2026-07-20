"""Product job handler for score recompute.

Closes the compute -> persist loop: given an affected local health day, the
handler assembles the recovery surface (the same pure path the read surfaces
use) and persists it as a versioned score row. Persistence is idempotent by
dependency hash, so a redundant recompute writes no new version — which is what
makes the job safe to retry after a lease expires mid-run.

The handler is framework-free. It depends on the recovery surface service
(application) and a narrow score-writer protocol, defined here rather than in
``ports`` because the write payload is the application ``RecoverySurface``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from akunaki.application.recovery_surface import RecoverySurface, RecoverySurfaceService
from akunaki.domain.jobs import SCORE_RECOMPUTE_JOB_TYPE, JobClaim
from akunaki.domain.retry import PermanentJobError

logger = logging.getLogger("akunaki.score_handlers")

__all__ = ["SCORE_RECOMPUTE_JOB_TYPE", "ScoreRecomputeHandler", "ScoreWriterPort"]


class ScoreWriteOutcomeLike(Protocol):
    """What one score write persisted."""

    @property
    def is_new_version(self) -> bool:
        """True when a new score version was appended."""
        ...


class ScoreWriterPort(Protocol):
    """Persist a computed recovery surface as a versioned score."""

    def write_recovery_score(
        self,
        *,
        score_id: str,
        tenant_id: str,
        surface: RecoverySurface,
        new_factor_id: Callable[[], str],
        as_of_at: datetime | None,
        now: datetime,
    ) -> ScoreWriteOutcomeLike:
        """Write the recovery score, superseding any differing current row."""
        ...


class ScoreRecomputeHandler:
    """Recompute and persist the recovery score for one local health day.

    Keyed by ``local_health_day``; a retry recomputes the same day and the
    persistence layer dedupes identical results. The score is written for every
    status — an ``insufficient`` day is a real, disclosed outcome worth storing,
    not an absence.
    """

    def __init__(
        self,
        *,
        recovery: RecoverySurfaceService,
        scores: ScoreWriterPort,
        new_id: Callable[[], str],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._recovery = recovery
        self._scores = scores
        self._new_id = new_id
        self._clock = clock

    def __call__(self, claim: JobClaim) -> None:
        """Execute one recompute job."""
        local_health_day = _parse_recompute_payload(claim.payload_json)
        now = self._clock()

        surface = self._recovery.recovery_for_day(
            tenant_id=claim.tenant_id,
            local_health_day=local_health_day,
        )
        outcome = self._scores.write_recovery_score(
            score_id=self._new_id(),
            tenant_id=claim.tenant_id,
            surface=surface,
            new_factor_id=self._new_id,
            as_of_at=now,
            now=now,
        )

        logger.info(
            "recomputed recovery score",
            extra={
                "local_health_day": local_health_day,
                "status": surface.status.value,
                "score": surface.score,
                "wrote_new_version": outcome.is_new_version,
            },
        )


def _parse_recompute_payload(payload_json: str) -> str:
    """Extract the local health day, or raise a permanent error.

    A malformed payload will not fix itself on retry, so it is permanent rather
    than transient.
    """
    try:
        parsed = json.loads(payload_json)
    except ValueError as exc:
        msg = "payload is not valid json"
        raise PermanentJobError(msg) from exc
    day = parsed.get("local_health_day") if isinstance(parsed, dict) else None
    if not isinstance(day, str) or len(day) != 10:
        msg = "payload must contain a YYYY-MM-DD local_health_day"
        raise PermanentJobError(msg)
    return day
