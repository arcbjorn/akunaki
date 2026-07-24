"""Source-selection persistence (daily-metric slice).

Records the deterministic per-day source-precedence decision as a versioned,
auditable row: exactly one **current** ``source_selections`` per
``(tenant, metric_family, grain_key)`` (grain_key = the local health day), plus a
``source_selection_candidates`` row per competing provider fact for the "Why".

Writing a decision **supersedes** any differing current row (append a new
version, retire the old), so the selection history stays queryable. Providers
are never blended: the decision names one selected fact (or ``missing`` with a
reason), and the losers are visible only as candidates — never a fallback.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.models import SourceSelection, SourceSelectionCandidate
from akunaki.domain.jobs import require_aware, to_utc_rfc3339

GRANULARITY_DAILY = "daily_metric"


@dataclass(frozen=True, slots=True)
class CandidateSpec:
    """One competing provider fact for a selection decision."""

    fact_record_id: str
    rank: int
    eligibility: str  # 'eligible' | 'ineligible'
    reason: str


@dataclass(frozen=True, slots=True)
class SelectionSpec:
    """A per-day source-selection decision to persist."""

    metric_family: str
    local_health_day: str
    selected_fact_record_id: str | None
    selection_reason: str
    missing_reason: str | None
    candidates: tuple[CandidateSpec, ...]


@dataclass(frozen=True, slots=True)
class SelectionWritten:
    """Outcome of recording a selection."""

    selection_id: str
    version_n: int
    is_new_version: bool


class SourceSelectionRepository:
    """Persist versioned daily source-selection decisions and their candidates."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def record_daily_selection(
        self,
        *,
        selection_id: str,
        tenant_id: str,
        policy_version: str,
        spec: SelectionSpec,
        new_candidate_id: Callable[[], str],
        now: datetime,
    ) -> SelectionWritten:
        """Record a daily-metric decision, superseding any differing current row.

        Idempotent by the decision's content: if the current row already selects
        the same fact for the same reason with the same candidate set, no new
        version is written.
        """
        if not selection_id or not tenant_id:
            msg = "selection_id and tenant_id must be non-empty"
            raise ValueError(msg)
        _validate_reason(spec)

        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))
        grain_key = spec.local_health_day

        with self._session_factory() as session, session.begin():
            current = session.execute(
                select(SourceSelection).where(
                    SourceSelection.tenant_id == tenant_id,
                    SourceSelection.metric_family == spec.metric_family,
                    SourceSelection.granularity == GRANULARITY_DAILY,
                    SourceSelection.grain_key == grain_key,
                    SourceSelection.is_current == 1,
                )
            ).scalar_one_or_none()

            if current is not None and self._matches(session, current, spec):
                return SelectionWritten(
                    selection_id=current.id,
                    version_n=current.version_n,
                    is_new_version=False,
                )

            next_version = 1
            if current is not None:
                next_version = current.version_n + 1
                session.execute(
                    update(SourceSelection)
                    .where(SourceSelection.id == current.id)
                    .values(is_current=0, superseded_by=selection_id)
                )
                session.flush()

            session.add(
                SourceSelection(
                    id=selection_id,
                    tenant_id=tenant_id,
                    metric_family=spec.metric_family,
                    granularity=GRANULARITY_DAILY,
                    grain_key=grain_key,
                    local_health_day=spec.local_health_day,
                    selected_fact_record_id=spec.selected_fact_record_id,
                    source_policy_version_id=policy_version,
                    selection_reason=spec.selection_reason,
                    missing_reason=spec.missing_reason,
                    version_n=next_version,
                    is_current=1,
                    superseded_by=None,
                    created_at=now_s,
                )
            )
            session.flush()
            for candidate in spec.candidates:
                session.add(
                    SourceSelectionCandidate(
                        id=new_candidate_id(),
                        tenant_id=tenant_id,
                        source_selection_id=selection_id,
                        fact_record_id=candidate.fact_record_id,
                        rank=candidate.rank,
                        eligibility=candidate.eligibility,
                        reason=candidate.reason,
                    )
                )

            return SelectionWritten(
                selection_id=selection_id,
                version_n=next_version,
                is_new_version=True,
            )

    def _matches(self, session: Session, current: SourceSelection, spec: SelectionSpec) -> bool:
        """Whether the current row already encodes exactly this decision."""
        if (
            current.selected_fact_record_id != spec.selected_fact_record_id
            or current.selection_reason != spec.selection_reason
            or current.missing_reason != spec.missing_reason
        ):
            return False
        existing = session.execute(
            select(
                SourceSelectionCandidate.fact_record_id,
                SourceSelectionCandidate.rank,
                SourceSelectionCandidate.eligibility,
                SourceSelectionCandidate.reason,
            ).where(SourceSelectionCandidate.source_selection_id == current.id)
        ).all()
        want = {(c.fact_record_id, c.rank, c.eligibility, c.reason) for c in spec.candidates}
        return set(existing) == want

    def current_selection(
        self, *, tenant_id: str, metric_family: str, local_health_day: str
    ) -> SourceSelection | None:
        """Return the current decision for a day, or None."""
        with self._session_factory() as session:
            return session.execute(
                select(SourceSelection).where(
                    SourceSelection.tenant_id == tenant_id,
                    SourceSelection.metric_family == metric_family,
                    SourceSelection.granularity == GRANULARITY_DAILY,
                    SourceSelection.grain_key == local_health_day,
                    SourceSelection.is_current == 1,
                )
            ).scalar_one_or_none()


def _validate_reason(spec: SelectionSpec) -> None:
    """Enforce the missing-authoritative consistency rule before insert."""
    if spec.selection_reason == "missing_authoritative":
        if spec.selected_fact_record_id is not None or spec.missing_reason is None:
            msg = "missing_authoritative requires no selected fact and a missing_reason"
            raise ValueError(msg)
    elif spec.selected_fact_record_id is None or spec.missing_reason is not None:
        msg = "a non-missing selection requires a selected fact and no missing_reason"
        raise ValueError(msg)
