"""Subjective check-in persistence with versioning.

A check-in is **never rewritten in place**. Recording a check-in for a day that
already has one supersedes the prior version and appends a new current row, so
the history of what a user reported stays auditable. One current row per
``(tenant_id, local_health_day)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.models import SubjectiveCheckIn
from akunaki.domain.jobs import require_aware, to_utc_rfc3339
from akunaki.domain.subjective import SubjectiveInputs


@dataclass(frozen=True, slots=True)
class CheckInWriteOutcome:
    """What one check-in write persisted."""

    check_in_id: str
    version_n: int
    superseded_id: str | None = None


class CheckInRepository:
    """Persist versioned subjective check-ins and read the current one."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def record_check_in(
        self,
        *,
        check_in_id: str,
        tenant_id: str,
        local_health_day: str,
        inputs: SubjectiveInputs,
        completed_at: datetime,
        now: datetime,
    ) -> CheckInWriteOutcome:
        """Record a completed check-in, superseding any current one for the day."""
        if not check_in_id or not tenant_id:
            msg = "check_in_id and tenant_id must be non-empty"
            raise ValueError(msg)
        if len(local_health_day) != 10:
            msg = "local_health_day must be YYYY-MM-DD"
            raise ValueError(msg)

        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))
        completed_s = to_utc_rfc3339(require_aware(completed_at, field_name="completed_at"))

        with self._session_factory() as session, session.begin():
            current = session.execute(
                select(SubjectiveCheckIn).where(
                    SubjectiveCheckIn.tenant_id == tenant_id,
                    SubjectiveCheckIn.local_health_day == local_health_day,
                    SubjectiveCheckIn.is_current == 1,
                )
            ).scalar_one_or_none()

            next_version = 1
            superseded_id: str | None = None
            if current is not None:
                next_version = current.version_n + 1
                superseded_id = current.id
                # Retire the old version before inserting: the partial unique
                # index permits only one current row per key.
                session.execute(
                    update(SubjectiveCheckIn)
                    .where(SubjectiveCheckIn.id == current.id)
                    .values(
                        is_current=0,
                        superseded_by=check_in_id,
                        superseded_at=now_s,
                    )
                )
                session.flush()

            session.add(
                SubjectiveCheckIn(
                    id=check_in_id,
                    tenant_id=tenant_id,
                    local_health_day=local_health_day,
                    energy_n=inputs.energy_n,
                    stress_n=inputs.stress_n,
                    symptom_burden_n=inputs.symptom_burden_n,
                    completed_at=completed_s,
                    version_n=next_version,
                    is_current=1,
                    superseded_by=None,
                    superseded_at=None,
                    created_at=now_s,
                )
            )

            return CheckInWriteOutcome(
                check_in_id=check_in_id,
                version_n=next_version,
                superseded_id=superseded_id,
            )

    def current_check_in_inputs(
        self, *, tenant_id: str, local_health_day: str
    ) -> SubjectiveInputs | None:
        """Return the current completed check-in's inputs for a day, or None.

        A row without ``completed_at`` is not a completed check-in and is
        treated as absent — the subjective component is then omitted.
        """
        with self._session_factory() as session:
            row = session.execute(
                select(SubjectiveCheckIn).where(
                    SubjectiveCheckIn.tenant_id == tenant_id,
                    SubjectiveCheckIn.local_health_day == local_health_day,
                    SubjectiveCheckIn.is_current == 1,
                )
            ).scalar_one_or_none()
            if row is None or row.completed_at is None:
                return None
            return SubjectiveInputs(
                energy_n=row.energy_n,
                stress_n=row.stress_n,
                symptom_burden_n=row.symptom_burden_n,
            )
