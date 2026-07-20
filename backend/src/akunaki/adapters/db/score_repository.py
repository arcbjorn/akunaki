"""Score persistence with versioning.

Scores are **never rewritten in place**. Writing a score whose inputs differ
from the current version supersedes it and appends a new one, so history stays
auditable across formula and policy changes. Writing an identical result — same
``dependency_hash`` — is a no-op, which is what makes recompute idempotent.

The dependency hash is computed over the disclosed outputs (status, score,
confidence, coverage, and the present factors), so a recompute that produces the
same disclosed result writes no new version even if incidental metadata differs.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.models import DailyHealthScore, ScoreFactor
from akunaki.application.recovery_surface import (
    RecoverySurface,
    StoredRecoveryScore,
    StoredScoreFactor,
)
from akunaki.domain.jobs import require_aware, to_utc_rfc3339


@dataclass(frozen=True, slots=True)
class ScoreWriteOutcome:
    """What one score write persisted."""

    daily_health_score_id: str
    version_n: int
    is_new_version: bool
    superseded_id: str | None = None


class ScoreRepository:
    """Persist versioned daily scores and their signed factors."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def write_recovery_score(
        self,
        *,
        score_id: str,
        tenant_id: str,
        surface: RecoverySurface,
        new_factor_id: Callable[[], str],
        as_of_at: datetime | None,
        now: datetime,
    ) -> ScoreWriteOutcome:
        """Persist a recovery surface, superseding any differing current row.

        An identical recompute (same dependency hash) is a no-op. A changed
        result retires the current row and appends a new version with fresh
        factor rows.
        """
        if not score_id or not tenant_id:
            msg = "score_id and tenant_id must be non-empty"
            raise ValueError(msg)

        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))
        as_of_s = (
            to_utc_rfc3339(require_aware(as_of_at, field_name="as_of_at")) if as_of_at else None
        )
        dependency_hash = _dependency_hash(surface)

        with self._session_factory() as session, session.begin():
            current = session.execute(
                select(DailyHealthScore).where(
                    DailyHealthScore.tenant_id == tenant_id,
                    DailyHealthScore.local_health_day == surface.local_health_day,
                    DailyHealthScore.score_code == surface.score_code,
                    DailyHealthScore.is_current == 1,
                )
            ).scalar_one_or_none()

            if current is not None and current.dependency_hash == dependency_hash:
                # Identical disclosed result: recompute is a no-op.
                return ScoreWriteOutcome(
                    daily_health_score_id=current.id,
                    version_n=current.version_n,
                    is_new_version=False,
                )

            next_version = 1
            superseded_id: str | None = None
            if current is not None:
                next_version = current.version_n + 1
                superseded_id = current.id
                # Retire the old version before inserting the new one: the
                # partial unique index permits only one current row per key.
                session.execute(
                    update(DailyHealthScore)
                    .where(DailyHealthScore.id == current.id)
                    .values(
                        is_current=0,
                        superseded_by=score_id,
                        superseded_at=now_s,
                    )
                )
                session.flush()

            session.add(
                DailyHealthScore(
                    id=score_id,
                    tenant_id=tenant_id,
                    local_health_day=surface.local_health_day,
                    score_code=surface.score_code,
                    status=surface.status.value,
                    score=surface.score,
                    available_weight=surface.available_weight,
                    confidence=surface.confidence,
                    formula_version=surface.formula_version,
                    dependency_hash=dependency_hash,
                    freshness_at=now_s,
                    as_of_at=as_of_s,
                    version_n=next_version,
                    is_current=1,
                    superseded_by=None,
                    superseded_at=None,
                    created_at=now_s,
                )
            )
            for factor in surface.factors:
                session.add(
                    ScoreFactor(
                        id=new_factor_id(),
                        daily_health_score_id=score_id,
                        tenant_id=tenant_id,
                        factor_code=factor.factor_code,
                        sign=_factor_sign(factor.magnitude, present=factor.present),
                        magnitude=factor.magnitude,
                        weight=factor.weight,
                        present=1 if factor.present else 0,
                    )
                )

            return ScoreWriteOutcome(
                daily_health_score_id=score_id,
                version_n=next_version,
                is_new_version=True,
                superseded_id=superseded_id,
            )

    def current_recovery_score(
        self, *, tenant_id: str, local_health_day: str
    ) -> DailyHealthScore | None:
        """Return the current recovery score row for a day, or None."""
        with self._session_factory() as session:
            return session.execute(
                select(DailyHealthScore).where(
                    DailyHealthScore.tenant_id == tenant_id,
                    DailyHealthScore.local_health_day == local_health_day,
                    DailyHealthScore.score_code == "recovery",
                    DailyHealthScore.is_current == 1,
                )
            ).scalar_one_or_none()

    def current_recovery_with_factors(
        self, *, tenant_id: str, local_health_day: str
    ) -> StoredRecoveryScore | None:
        """Return the current recovery score and its factor rows, or None.

        A single read: the score header plus every ``score_factor`` row that
        belongs to it, so the read surface can reconstruct the disclosed view
        (factors and gaps) without recomputing.
        """
        with self._session_factory() as session:
            row = session.execute(
                select(DailyHealthScore).where(
                    DailyHealthScore.tenant_id == tenant_id,
                    DailyHealthScore.local_health_day == local_health_day,
                    DailyHealthScore.score_code == "recovery",
                    DailyHealthScore.is_current == 1,
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            factor_rows = (
                session.execute(
                    select(ScoreFactor).where(
                        ScoreFactor.daily_health_score_id == row.id,
                    )
                )
                .scalars()
                .all()
            )
            factors = tuple(
                StoredScoreFactor(
                    factor_code=f.factor_code,
                    present=bool(f.present),
                    weight=f.weight,
                    magnitude=f.magnitude,
                )
                for f in factor_rows
            )
            return StoredRecoveryScore(
                local_health_day=row.local_health_day,
                score_code=row.score_code,
                status=row.status,
                score=row.score,
                available_weight=row.available_weight,
                confidence=row.confidence,
                formula_version=row.formula_version,
                freshness_at=row.freshness_at,
                version_n=row.version_n,
                factors=factors,
            )


def _factor_sign(magnitude: float, *, present: bool) -> int:
    """Direction a present factor pushes recovery: above midpoint helps.

    An absent factor has no direction (0). A present component score above the
    50 midpoint pushes recovery up (+1), below pushes down (-1), exactly at the
    midpoint is neutral (0).
    """
    if not present:
        return 0
    if magnitude > 50.0:
        return 1
    if magnitude < 50.0:
        return -1
    return 0


def _dependency_hash(surface: RecoverySurface) -> str:
    """Deterministic hash over a surface's disclosed outputs.

    Present factors are sorted by code so ordering never changes the hash; an
    absent factor is excluded, so gaining/losing an absent placeholder does not
    churn the version.
    """
    material = json.dumps(
        {
            "score_code": surface.score_code,
            "status": surface.status.value,
            "score": surface.score,
            "available_weight": surface.available_weight,
            "confidence": surface.confidence,
            "formula_version": surface.formula_version,
            "factors": sorted(
                (f.factor_code, f.weight, f.magnitude) for f in surface.factors if f.present
            ),
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
