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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from akunaki.application.anomaly_tracker import AnomalyTracker
from akunaki.application.recovery_inputs import RecoveryInputService
from akunaki.application.recovery_surface import RecoverySurface, RecoverySurfaceService
from akunaki.domain.jobs import SCORE_RECOMPUTE_JOB_TYPE, JobClaim
from akunaki.domain.retry import PermanentJobError

logger = logging.getLogger("akunaki.score_handlers")

__all__ = [
    "SCORE_RECOMPUTE_JOB_TYPE",
    "DerivationInputSpec",
    "DerivationWriterPort",
    "ScoreRecomputeHandler",
    "ScoreWriterPort",
]


@dataclass(frozen=True, slots=True)
class DerivationInputSpec:
    """One typed input to record for a derivation run (role + fact id)."""

    role: str
    fact_record_id: str


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
        derivation_run_id: str | None = ...,
    ) -> ScoreWriteOutcomeLike:
        """Write the recovery score, superseding any differing current row."""
        ...


class RunCreatedLike(Protocol):
    """A created derivation run with its opaque token."""

    @property
    def run_id(self) -> str:
        """The run's id."""
        ...


class DerivationWriterPort(Protocol):
    """Record a derivation run with its typed inputs and an opaque token."""

    def create_run(
        self,
        *,
        run_id: str,
        tenant_id: str,
        artifact_kind: str,
        local_health_day: str | None,
        formula_version: str,
        dependency_hash: str,
        confidence: float | None,
        freshness_at: str | None,
        as_of_at: str | None,
        status: str,
        inputs: list[DerivationInputSpec],
        generate_token: Callable[[], str],
        new_input_id: Callable[[], str],
        now: datetime,
    ) -> RunCreatedLike:
        """Create a run and mint its opaque provenance token."""
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
        inputs: RecoveryInputService | None = None,
        tracker: AnomalyTracker | None = None,
        derivations: DerivationWriterPort | None = None,
        generate_token: Callable[[], str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._recovery = recovery
        self._scores = scores
        self._new_id = new_id
        self._inputs = inputs
        self._tracker = tracker
        self._derivations = derivations
        self._generate_token = generate_token
        self._clock = clock

    def __call__(self, claim: JobClaim) -> None:
        """Execute one recompute job."""
        local_health_day = _parse_recompute_payload(claim.payload_json)
        now = self._clock()

        surface = self._recovery.recovery_for_day(
            tenant_id=claim.tenant_id,
            local_health_day=local_health_day,
        )

        # Record a derivation run for the score when the writer is wired, so the
        # served value can be traced through an opaque provenance token. Typed
        # fact-id inputs are not threaded yet (the input service exposes roles,
        # not fact ids), so the run carries its versions/status/coverage as the
        # traceable artifact and no per-input rows for now.
        run_id: str | None = None
        if self._derivations is not None and self._generate_token is not None:
            created = self._derivations.create_run(
                run_id=self._new_id(),
                tenant_id=claim.tenant_id,
                artifact_kind="score",
                local_health_day=local_health_day,
                formula_version=surface.formula_version,
                dependency_hash="",
                confidence=surface.confidence,
                freshness_at=surface.freshness_at,
                as_of_at=None,
                status=surface.status.value,
                inputs=[],
                generate_token=self._generate_token,
                new_input_id=self._new_id,
                now=now,
            )
            run_id = created.run_id

        outcome = self._scores.write_recovery_score(
            score_id=self._new_id(),
            tenant_id=claim.tenant_id,
            surface=surface,
            new_factor_id=self._new_id,
            as_of_at=now,
            now=now,
            derivation_run_id=run_id,
        )

        # Detect and track anomalies for the day when both collaborators are
        # wired. The anomaly state machine advances one day per recompute.
        if self._inputs is not None and self._tracker is not None:
            signals = self._inputs.feature_signals(
                tenant_id=claim.tenant_id,
                local_health_day=local_health_day,
            )
            self._tracker.track(
                tenant_id=claim.tenant_id,
                local_health_day=local_health_day,
                signals=signals,
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
