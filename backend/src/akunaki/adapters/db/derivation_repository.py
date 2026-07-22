"""Derivation-run persistence and opaque provenance lookup.

A derivation run records the exact inputs and versions a derived value came
from. Its opaque provenance token is a **public but unguessable** handle: a day
response includes it every time (so it is stored in the clear, not a secret),
yet it cannot be enumerated, and it exposes no table or run id. A lookup is an
index probe on the token.

Provenance responses expose lineage **roles and versions**, never table or raw
ids: the derived-value chain is auditable without leaking the storage shape.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.models import DerivationInput, DerivationRun
from akunaki.application.score_handlers import DerivationInputSpec
from akunaki.domain.jobs import require_aware, to_utc_rfc3339


@dataclass(frozen=True, slots=True)
class RunCreated:
    """A newly created derivation run and its one-time provenance token."""

    run_id: str
    provenance_token: str


@dataclass(frozen=True, slots=True)
class ProvenanceInput:
    """One disclosed lineage input (role only — never an id)."""

    role: str


@dataclass(frozen=True, slots=True)
class Provenance:
    """The disclosed lineage for a derivation run.

    Carries versions, status, and freshness — no table, raw, or run ids.
    """

    artifact_kind: str
    local_health_day: str | None
    formula_version: str
    status: str
    confidence: float | None
    freshness_at: str | None
    as_of_at: str | None
    inputs: tuple[ProvenanceInput, ...]


class DerivationRepository:
    """Persist derivation runs and resolve opaque provenance tokens."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

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
    ) -> RunCreated:
        """Create a run with its typed inputs; return the one-time token."""
        if not run_id or not tenant_id:
            msg = "run_id and tenant_id must be non-empty"
            raise ValueError(msg)

        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))
        token = generate_token()

        with self._session_factory() as session, session.begin():
            session.add(
                DerivationRun(
                    id=run_id,
                    tenant_id=tenant_id,
                    artifact_kind=artifact_kind,
                    local_health_day=local_health_day,
                    formula_version=formula_version,
                    dependency_hash=dependency_hash,
                    confidence=confidence,
                    freshness_at=freshness_at,
                    as_of_at=as_of_at,
                    status=status,
                    provenance_token=token,
                    superseded_by=None,
                    created_at=now_s,
                )
            )
            for spec in inputs:
                session.add(
                    DerivationInput(
                        id=new_input_id(),
                        derivation_run_id=run_id,
                        tenant_id=tenant_id,
                        role=spec.role,
                        fact_record_id=spec.fact_record_id,
                    )
                )

        return RunCreated(run_id=run_id, provenance_token=token)

    def resolve_token(self, *, tenant_id: str, token: str) -> Provenance | None:
        """Resolve an opaque provenance token to disclosed lineage, or None.

        Scoped by tenant: a token from one tenant cannot read another's lineage
        even if presented, and an unknown token is indistinguishable from a
        cross-tenant one (both None).
        """
        if not token:
            return None
        with self._session_factory() as session:
            run = session.execute(
                select(DerivationRun).where(
                    DerivationRun.tenant_id == tenant_id,
                    DerivationRun.provenance_token == token,
                )
            ).scalar_one_or_none()
            if run is None:
                return None
            input_rows = (
                session.execute(
                    select(DerivationInput.role).where(DerivationInput.derivation_run_id == run.id)
                )
                .scalars()
                .all()
            )
            return Provenance(
                artifact_kind=run.artifact_kind,
                local_health_day=run.local_health_day,
                formula_version=run.formula_version,
                status=run.status,
                confidence=run.confidence,
                freshness_at=run.freshness_at,
                as_of_at=run.as_of_at,
                inputs=tuple(ProvenanceInput(role=role) for role in sorted(input_rows)),
            )
