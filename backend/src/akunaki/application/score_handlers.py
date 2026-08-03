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
from typing import Any, Protocol

from akunaki.application.anomaly_tracker import AnomalyTracker
from akunaki.application.recovery_inputs import RecoveryInputService
from akunaki.application.recovery_surface import RecoverySurface, RecoverySurfaceService
from akunaki.domain.derivation_roles import entity_type_for_component
from akunaki.domain.jobs import SCORE_RECOMPUTE_JOB_TYPE, JobClaim
from akunaki.domain.recovery import ComponentCode
from akunaki.domain.retry import PermanentJobError
from akunaki.ports.unit_of_work import FencedUnitOfWorkPort

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
        session: Any = ...,
    ) -> ScoreWriteOutcomeLike:
        """Write the recovery score, superseding any differing current row.

        ``session`` enlists the write in a caller's transaction. It is typed
        loosely here on purpose: ``application`` must not import SQLAlchemy, so
        the handler only ever forwards the opaque handle the unit of work
        gave it.
        """
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
        unit_of_work: FencedUnitOfWorkPort | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._recovery = recovery
        self._scores = scores
        self._new_id = new_id
        self._inputs = inputs
        self._tracker = tracker
        self._derivations = derivations
        self._generate_token = generate_token
        self._unit_of_work = unit_of_work
        self._clock = clock

    def _derivation_inputs(
        self,
        *,
        tenant_id: str,
        local_health_day: str,
        surface: RecoverySurface,
    ) -> list[DerivationInputSpec]:
        """Name the facts this score was derived from, one row per fact.

        Only **present** components contribute: a component that was omitted
        (absent value, immature baseline) read no fact, so recording one would
        assert an input the formula never used. The ``role`` is the component
        code, matching the factor rows the score already discloses.

        Returns an empty list when the input service is not wired — the run is
        still recorded, just without per-input rows, exactly as before.
        """
        if self._inputs is None:
            return []

        present_codes = {factor.factor_code for factor in surface.factors if factor.present}
        if not present_codes:
            return []

        facts_by_entity = self._inputs.fact_ids_for_day(
            tenant_id=tenant_id,
            local_health_day=local_health_day,
        )

        specs: list[DerivationInputSpec] = []
        seen: set[tuple[str, str]] = set()
        for code in sorted(present_codes):
            try:
                component = ComponentCode(code)
            except ValueError:
                # A factor code outside the component vocabulary is not
                # fact-sourced; skip rather than guess at a table.
                continue
            entity_type = entity_type_for_component(component)
            if entity_type is None:
                continue
            for fact_id in facts_by_entity.get(entity_type, []):
                key = (code, fact_id)
                if key in seen:
                    continue
                seen.add(key)
                specs.append(DerivationInputSpec(role=code, fact_record_id=fact_id))
        return specs

    def _persist(
        self,
        *,
        claim: JobClaim,
        local_health_day: str,
        surface: RecoverySurface,
        now: datetime,
        session: Any,
    ) -> ScoreWriteOutcomeLike:
        """Record the derivation run, write the score, and advance anomalies.

        Every write here belongs to the same unit of work: when ``session`` is
        supplied they commit or roll back together with the lease check that
        authorized them.
        """
        # Record a derivation run for the score when the writer is wired, so the
        # served value can be traced through an opaque provenance token, and
        # name the facts it was derived from as typed inputs.
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
                inputs=self._derivation_inputs(
                    tenant_id=claim.tenant_id,
                    local_health_day=local_health_day,
                    surface=surface,
                ),
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
            session=session,
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

        return outcome

    def __call__(self, claim: JobClaim) -> None:
        """Execute one recompute job."""
        local_health_day = _parse_recompute_payload(claim.payload_json)
        now = self._clock()

        surface = self._recovery.recovery_for_day(
            tenant_id=claim.tenant_id,
            local_health_day=local_health_day,
        )

        # The persist step is the job's side effect. When a fenced unit of work
        # is wired it runs inside the transaction that re-checks the job lease,
        # so a worker whose lease expired mid-compute cannot supersede the row
        # the rightful owner wrote. Without one, it runs exactly as before.
        if self._unit_of_work is not None:
            outcome = self._unit_of_work.run_fenced(
                claim,
                lambda session: self._persist(
                    claim=claim,
                    local_health_day=local_health_day,
                    surface=surface,
                    now=now,
                    session=session,
                ),
                now=now,
            )
        else:
            outcome = self._persist(
                claim=claim,
                local_health_day=local_health_day,
                surface=surface,
                now=now,
                session=None,
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
